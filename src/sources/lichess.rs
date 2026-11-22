//! The Lichess broadcast feed: a round snapshot, or the live stream of its games.

use anyhow::Result;
use reqwest::Response;
use tokio::sync::mpsc::Sender;
use tokio_stream::StreamExt;
use tracing::error;
use tracing::info;
use tracing::warn;

use crate::broadcast::{round_meta, RoundMeta};
use crate::observation::{Clock, Observation, Payload};
use crate::pgn;
use crate::pgn::Position;
use crate::pgn::clocks::{ClockReading, TimeControl};
use crate::pgn::stream::GameStream;

async fn streaming_run(
    round_id: &str,
    clock: Clock,
    tx: Sender<Observation>,
    pos_tx: Sender<Position>,
    meta: Option<RoundMeta>,
) -> Result<()> {
    info!("Running streaming request mode");
    let url: String = format!(
        "https://lichess.org/api/stream/broadcast/round/{}.pgn",
        round_id
    );
    let res: Response = reqwest::get(url).await?;
    let stream = res.bytes_stream();
    tokio::pin!(stream);

    let meta = match meta {
        Some(m) => m,
        None => round_meta(round_id).await?,
    };
    let starts_at_ms = meta.starts_at_ms;

    // The round's stated control, when it stated one. A game carrying its own [TimeControl] header
    // still overrides this per game — see `move_times_for`.
    let (tc, tc_source) = match meta.time_control() {
        Some(t) => (t, "round metadata"),
        None => (TimeControl::default(), "assumed"),
    };
    let mut games = GameStream::new();
    let _ = tx
        .send(clock.note(format!(
            "lichess: stream open, round {round_id}, startsAt {starts_at_ms}, increment {}s from move {} ({tc_source})",
            tc.increment_secs, tc.increment_from_move
        )))
        .await;

    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;

        let update = games.update(&chunk);

        for info in update.infos {
            if tx.send(clock.stamp(info.into())).await.is_err() {
                warn!("lichess: log channel closed");
                return Ok(());
            }
        }

        for (id, fresh) in update.plies {
            let times = move_times_for(&games, &id, starts_at_ms, tc);

            for p in &fresh {
                let move_unix_ms = times
                    .get(p.ply as usize - 1)
                    .copied()
                    .unwrap_or(starts_at_ms);
                let record = clock.stamp(Payload::Ply {
                    game_id: id.clone(),
                    ply: p.ply,
                    san: p.san.clone(),
                    uci: p.uci.clone(),
                    fen: p.fen.clone(),
                    move_unix_ms,
                });
                if tx.send(record).await.is_err() {
                    warn!("lichess: log channel closed");
                    return Ok(());
                }
            }

            if games.is_finished(&id) == Some(false) {
                if let Some(pos) = games.position(&id) {
                    if pos_tx.send(pos).await.is_err() {
                        warn!("lichess: engine channel closed, stopping position feed");
                    }
                }
            }
        }
    }
    let _ = tx.send(clock.note("lichess: stream ended")).await;
    Ok(())
}

/// Reconstructed wall-clock time for every ply of a game, so a new ply can be looked up by index.
///
/// Elapsed time is cumulative, so this is computed over the whole game rather than the new plies.
fn move_times_for(
    games: &GameStream,
    id: &str,
    starts_at_ms: u64,
    tc: TimeControl,
) -> Vec<u64> {
    let Some(text) = games.get(id) else {
        return Vec::new();
    };
    let Some(game) = pgn::split(text).into_iter().next() else {
        return Vec::new();
    };
    // A game's own [TimeControl] header wins: it is per game and in the unambiguous PGN notation.
    // Most relayed rounds have none, which is why the round's metadata is the fallback.
    let tc = game
        .tag("TimeControl")
        .and_then(pgn::clocks::parse_time_control)
        .unwrap_or(tc);

    let all = pgn::moves::plies(&game);
    let readings: Vec<ClockReading> = all
        .iter()
        .filter_map(|p| {
            p.clk_secs.map(|clk_secs| ClockReading {
                move_no: p.move_no,
                white: p.white,
                clk_secs,
            })
        })
        .collect();
    if readings.len() != all.len() {
        return Vec::new();
    }
    let base = readings.first().map(|r| r.clk_secs).unwrap_or(0);
    pgn::clocks::move_times_ms(&readings, starts_at_ms, tc, base)
}

pub async fn run(
    round_id: &str,
    clock: Clock,
    tx: Sender<Observation>,
    pos_tx: Sender<Position>,
    stream_data: bool,
    meta: Option<RoundMeta>,
) -> Result<()> {
    let url: String = format!("https://lichess.org/api/broadcast/round/{}.pgn", round_id);
    let res: Response = reqwest::get(url).await?;
    let raw_games: String = res.error_for_status()?.text().await?;
    let s = pgn::status(&raw_games);
    info!(
        "round {round_id}: {} games, {} finished, {} in play",
        s.total, s.finished, s.in_progress
    );

    if stream_data {
        match streaming_run(round_id, clock, tx, pos_tx, meta).await {
            Ok(()) => println!("Stream end"),
            Err(e) => error!("lichess: {e:#}"),
        }
    } else {
        let active_games: String = pgn::take_unfinished(&raw_games, 1);
        println!("{active_games}")
    };
    Ok(())
}
