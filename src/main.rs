//! Wiring: start the sources, drain the channel into the log, shut down cleanly.
//!
//! The `select!` loop below is the skeleton of ticket T-05. It already supervises sources and
//! handles Ctrl-C; what it does not yet have is sources that do anything, because those are
//! T-02 to T-04.

use anyhow::Result;
use clap::Parser;
use chess_to_market::cli::Cli;
use chess_to_market::observation::Clock;
use chess_to_market::sink::JsonlSink;
use chess_to_market::sources;
use chess_to_market::Observation;
use tokio::sync::mpsc;
use tokio::task::JoinSet;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

/// Channel depth between the sources and the log.
///
/// Bounded on purpose. If the log cannot keep up, a bounded channel makes the producers wait,
/// which is visible; an unbounded one would absorb the backlog in memory and the symptom would
/// surface much later as something that looks unrelated.
const CHANNEL_DEPTH: usize = 1024;

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .with_target(false)
        .init();

    let clock = Clock::new();
    let (tx, mut rx) = mpsc::channel::<Observation>(CHANNEL_DEPTH);
    let mut sink = JsonlSink::open(&cli.out).await?;
    let mut sources = JoinSet::new();

    if let Some(path) = cli.replay.clone() {
        info!("replay mode: {}", path.display());
        let tx = tx.clone();
        sources.spawn(async move {
            let n = sources::replay::run(&path, tx).await?;
            Ok::<_, anyhow::Error>(format!("replay: {n} records"))
        });
    } else {
        info!("live mode");
        if let Some(round_id) = cli.round_id.clone() {
            let tx = tx.clone();
            sources.spawn(async move {
                sources::lichess::run(&round_id, clock, tx).await?;
                Ok::<_, anyhow::Error>("lichess: finished".to_string())
            });
        } else {
            warn!("no --round-id: no chess feed");
        }

        if !cli.asset_ids.is_empty() {
            let asset_ids = cli.asset_ids.clone();
            let tx = tx.clone();
            sources.spawn(async move {
                sources::polymarket::run(&asset_ids, clock, tx).await?;
                Ok::<_, anyhow::Error>("polymarket: finished".to_string())
            });
        } else {
            warn!("no --asset-id: no market feed");
        }

        if let Some(engine_path) = cli.engine_path.clone() {
            let (depth, multipv) = (cli.depth, cli.multipv);
            let tx = tx.clone();
            sources.spawn(async move {
                sources::engine::run(&engine_path, depth, multipv, clock, tx).await?;
                Ok::<_, anyhow::Error>("engine: finished".to_string())
            });
        } else {
            warn!("no --engine-path: no evaluations");
        }
    }

    // The loop below ends when every sender is gone, so this one must not linger.
    drop(tx);

    // Each arm is independent: a source that fails or finishes does not end the run, and the log
    // keeps a Note saying what happened.
    loop {
        tokio::select! {
            maybe_obs = rx.recv() => {
                match maybe_obs {
                    Some(obs) => sink.write(&obs).await?,
                    // Every sender dropped: all sources are done.
                    None => break,
                }
            }

            Some(joined) = sources.join_next() => {
                match joined {
                    Ok(Ok(msg)) => info!("{msg}"),
                    Ok(Err(e)) => error!("source failed: {e:#}"),
                    // A panicking source must be loud rather than a silently missing feed.
                    Err(e) => error!("source panicked: {e}"),
                }
            }

            _ = tokio::signal::ctrl_c() => {
                info!("ctrl-c: draining and shutting down");
                sources.abort_all();
                // Drain what is already queued so the log does not end mid-game for no reason.
                while let Some(obs) = rx.recv().await {
                    sink.write(&obs).await?;
                }
                break;
            }
        }
    }

    let written = sink.close().await?;
    info!("wrote {written} records to {}", cli.out.display());
    Ok(())
}
