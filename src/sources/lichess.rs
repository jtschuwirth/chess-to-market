//! The Lichess broadcast feed: a round snapshot, or the live stream of its games.

use anyhow::Result;
use reqwest::Response;
use tokio::sync::mpsc::Sender;
use tokio_stream::StreamExt;
use tracing::error;
use tracing::info;
use tracing::warn;

use crate::observation::{Clock, Observation};
use crate::pgn;
use crate::pgn::Position;
use crate::pgn::stream::GameStream;

async fn streaming_run(round_id: &str, pos_tx: Sender<Position>) -> Result<()> {
    info!("Running streaming request mode");
    let url: String = format!(
        "https://lichess.org/api/stream/broadcast/round/{}.pgn",
        round_id
    );
    let res: Response = reqwest::get(url).await?;
    let stream = res.bytes_stream();
    tokio::pin!(stream);

    let mut games = GameStream::new();

    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        for pos in games.push_live_positions(&chunk) {
            if pos_tx.send(pos).await.is_err() {
                warn!("lichess: engine channel closed, stopping position feed");
                break;
            }
        }
    }
    Ok(())
}

pub async fn run(
    round_id: &str,
    _clock: Clock,
    _tx: Sender<Observation>,
    pos_tx: Sender<Position>,
    stream_data: bool,
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
        match streaming_run(round_id, pos_tx).await {
            Ok(()) => println!("Stream end"),
            Err(e) => error!("lichess: {e:#}"),
        }
    } else {
        let active_games: String = pgn::take_unfinished(&raw_games, 1);
        println!("{active_games}")
    };
    Ok(())
}
