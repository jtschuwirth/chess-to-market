//! Wiring: start the sources, drain the channel into the log, shut down cleanly.
//!
//! The `select!` loop below is the skeleton of ticket T-05. It already supervises sources and
//! handles Ctrl-C; what it does not yet have is sources that do anything, because those are
//! T-02 to T-04.

use anyhow::Result;
use chess_to_market::pgn::Position;
use chess_to_market::Observation;
use chess_to_market::broadcast::{round_meta, RoundMeta};
use chess_to_market::cli::Cli;
use chess_to_market::control;
use chess_to_market::market::{self, Market};
use chess_to_market::observation::Clock;
use chess_to_market::sink::JsonlSink;
use chess_to_market::sources;
use clap::Parser;
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

const POSITION_DEPTH: usize = 64;

/// Where this run writes, and what round it is following.
///
/// `--out` wins outright; otherwise a round gets its own file under `--log-dir`. Resolving it here,
/// before the sink opens, is what keeps a run's first record out of the wrong file.
///
/// A round whose metadata cannot be fetched is fatal rather than a fallback to a shared log: a bad
/// round id should say so, not quietly append a run's worth of records somewhere unexpected.
async fn resolve_log(cli: &Cli) -> Result<(std::path::PathBuf, Option<RoundMeta>)> {
    let round = match (&cli.round_id, cli.is_replay()) {
        (Some(id), false) => Some(round_meta(id).await?),
        _ => None,
    };
    let path = match (&cli.out, &round) {
        (Some(out), _) => out.clone(),
        (None, Some(meta)) => meta.log_path(&cli.log_dir),
        (None, None) => cli.log_dir.join("run.jsonl"),
    };
    Ok((path, round))
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .with_target(false)
        .init();

    let clock = Clock::new();
    let (tx, mut rx) = mpsc::channel::<Observation>(CHANNEL_DEPTH);
    let (pos_tx, pos_rx) = mpsc::channel::<Position>(POSITION_DEPTH);

    // Resolved before anything is written, so the first record already lands in the right file.
    let (out, round) = resolve_log(&cli).await?;
    let mut sink = JsonlSink::open(&out).await?;
    info!("log: {}", out.display());
    if let Some(meta) = &round {
        let _ = tx.send(clock.note(meta.describe())).await;
    }
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
            let pos_tx = pos_tx.clone();
            let stream = cli.stream;
            let meta = round.clone();
            sources.spawn(async move {
                sources::lichess::run(&round_id, clock, tx, pos_tx, stream, meta).await?;
                Ok::<_, anyhow::Error>("lichess: finished".to_string())
            });
        } else {
            warn!("no --round-id: no chess feed");
        }

        // Markets come from two places: the command line, and a control file the dashboard
        // writes while the program runs. The file is desired state, so it wins — the flag is the
        // starting point, not a separate list to merge.
        let control_path = control::path_for(&out);
        let from_flags = market::group(&cli.markets);
        let (market_tx, market_rx) = mpsc::channel::<Vec<Market>>(8);

        if !from_flags.is_empty() {
            info!("market: {}", sources::polymarket::describe(&from_flags));
        }
        info!("market control: {}", control_path.display());

        // The flags seed the file the first time; after that the file is the only source, so the
        // dashboard can disable a market that was started from the command line.
        if control::seed(&control_path, &from_flags)? {
            info!("market control: seeded from --market");
        }

        {
            let tx = tx.clone();
            sources.spawn(async move {
                sources::polymarket::run(&[], clock, tx, market_rx).await?;
                Ok::<_, anyhow::Error>("polymarket: finished".to_string())
            });
        }
        sources.spawn(async move {
            control::watch(control_path, market_tx).await?;
            Ok::<_, anyhow::Error>("control: finished".to_string())
        });

        if let Some(engine_path) = cli.engine_path.clone() {
            let (depth, multipv) = (cli.depth, cli.multipv);
            let tx = tx.clone();
            sources.spawn(async move {
                sources::engine::run(&engine_path, depth, multipv, clock, tx, pos_rx).await?;
                Ok::<_, anyhow::Error>("engine: finished".to_string())
            });
        } else {
            warn!("no --engine-path: no evaluations");
        }
    }

    // The loop below ends when every sender is gone, so these must not linger.
    drop(tx);
    drop(pos_tx);

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
    info!("wrote {written} records to {}", out.display());
    Ok(())
}
