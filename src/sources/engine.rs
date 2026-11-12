//! Stockfish over UCI. **Ticket T-04 — not implemented.**
//!
//! # What you are building
//!
//! Stockfish as a `tokio::process::Command` child with piped stdin and stdout. It is an external
//! binary, not a crate — install it and pass `--engine-path`. Do not vendor it.
//!
//! ```text
//! → uci                                ← … uciok
//! → setoption name MultiPV value 2
//! → isready                            ← readyok
//! → position startpos moves e2e4 e7e5 …
//! → go depth 18                        ← info depth … multipv 1 score cp … pv …
//!                                      ← info depth … multipv 2 score cp … pv …
//!                                      ← bestmove …
//! ```
//!
//! Read until `bestmove`, keeping the last `info` line seen for each `multipv` at the deepest
//! depth. The difference between multipv 1 and 2 is the only-move gap.
//!
//! # Three things that will bite
//!
//! - **`score cp` and `score mate` are different kinds of thing.** Storing a mate as a large
//!   centipawn number will quietly wreck every evaluation delta that spans it.
//! - **Normalise to white.** Stockfish scores from the side to move, so an unnormalised series
//!   alternates sign every ply and every swing you compute is noise.
//! - **Only one `go` outstanding at a time.** The engine is one serialised resource; the queue in
//!   front of it is the channel from the chess feed.
//!
//! Handshake with a timeout. A wrong `--engine-path` must fail with a clear message rather than
//! hanging, and "the program hung" is the least debuggable failure you can ship.
//!
//! Send one [`Payload::Eval`](crate::Payload::Eval) per evaluated position.

use anyhow::Result;
use std::path::Path;
use tokio::sync::mpsc::Sender;

use crate::observation::{Clock, Observation};

pub async fn run(
    engine_path: &Path,
    depth: u32,
    clock: Clock,
    tx: Sender<Observation>,
) -> Result<()> {
    let _ = tx
        .send(clock.note(format!(
            "engine: not implemented (T-04). path={} depth={depth}",
            engine_path.display()
        )))
        .await;
    Ok(())
}
