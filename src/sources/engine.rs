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
//! Send one [`Payload::Eval`](crate::Payload::Eval) **per completed depth of interest** (12 and
//! 16), each carrying `wdl_white` from `UCI_ShowWDL` normalised to white, `search_ms`, and the
//! MultiPV `lines` with their own per-mille `wdl` from the side to move.
//!
//! `wdl_white` is not optional. Everything downstream reasons in probability space; `cp_white` is
//! kept for the log, not for decisions.

use anyhow::Result;
use std::{path::Path, process::Stdio};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc::{Receiver, Sender};
use tracing::info;

use crate::observation::{Clock, Observation};
use crate::pgn::Position;

pub async fn run(
    engine_path: &Path,
    depth: u32,
    multipv: u32,
    clock: Clock,
    tx: Sender<Observation>,
    mut pos_rx: Receiver<Position>,
) -> Result<()> {
    info!("Starting engine");
    let mut child = Command::new(engine_path)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;

    let mut stdin = child.stdin.take().expect("piped");
    let mut lines = BufReader::new(child.stdout.take().expect("piped")).lines();

    // handshake: uci -> uciok, setoption, isready -> readyok

    // ---- per position ----
    while let Some(pos) = pos_rx.recv().await {
        stdin.write_all(uci_position(&pos).as_bytes()).await?;
        stdin
            .write_all(format!("go depth {depth}\n").as_bytes())
            .await?;

        while let Some(line) = lines.next_line().await? {
            // collect `info depth …` lines at the depths you care about
            if line.starts_with("bestmove") {
                break;
            }
        }
        // emit Eval observations via tx
    }
    Ok(())
}

/// The UCI `position` command for a game state.
fn uci_position(pos: &Position) -> String {
    let base = match &pos.start_fen {
        Some(fen) => format!("position fen {fen}"),
        None => "position startpos".to_string(),
    };
    if pos.moves.is_empty() {
        format!("{base}\n")
    } else {
        format!("{base} moves {}\n", pos.moves.join(" "))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pos(start_fen: Option<&str>, moves: &[&str]) -> Position {
        Position {
            game_id: "g".into(),
            ply: moves.len() as u32,
            start_fen: start_fen.map(str::to_string),
            moves: moves.iter().map(|m| m.to_string()).collect(),
        }
    }

    #[test]
    fn startpos_with_moves() {
        assert_eq!(
            uci_position(&pos(None, &["e2e4", "e7e5"])),
            "position startpos moves e2e4 e7e5\n"
        );
    }

    #[test]
    fn startpos_with_no_moves_omits_the_keyword() {
        assert_eq!(uci_position(&pos(None, &[])), "position startpos\n");
    }

    #[test]
    fn a_set_up_position_keeps_its_history() {
        let fen = "8/8/8/4k3/8/8/4K3/8 w - - 0 1";
        assert_eq!(
            uci_position(&pos(Some(fen), &["e2e3"])),
            format!("position fen {fen} moves e2e3\n")
        );
    }
}
