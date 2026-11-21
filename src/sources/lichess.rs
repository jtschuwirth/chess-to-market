//! The Lichess broadcast feed: a round snapshot, or the live stream of its games.

use anyhow::Result;
use reqwest::Response;
use tokio::sync::mpsc::Sender;
use tokio_stream::StreamExt;
use tracing::error;
use tracing::info;

use crate::observation::{Clock, Observation};
use crate::pgn;
use crate::pgn::stream::GameStream;

async fn streaming_run(round_id: &str) -> Result<()> {
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
        for id in games.push(&chunk) {
            if let Some(pgn_text) = games.get(&id) {
                println!("{}", pgn_text)
            }
        }
    }
    Ok(())
}

pub async fn run(
    round_id: &str,
    _clock: Clock,
    _tx: Sender<Observation>,
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
        match streaming_run(round_id).await {
            Ok(()) => println!("Stream end"),
            Err(e) => error!("lichess: {e:#}"),
        }
    } else {
        let active_games: String = pgn::take_unfinished(&raw_games, 1);
        println!("{active_games}")
    };
    Ok(())
}
