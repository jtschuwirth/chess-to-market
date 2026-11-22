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
//! Keep the last `info` line seen for each `multipv` at each depth. The difference between
//! multipv 1 and 2 is the only-move gap.
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
//! **As each depth completes, not at the end.** A depth is final once a line at a greater depth
//! arrives. Collecting them all and sending at `bestmove` makes depth 12 — which finishes in about
//! 47 ms — arrive with depth 16's 630 ms, so the cheap early verdict buys nothing and there is no
//! reason to compute it.
//!
//! `wdl_white` is not optional. Everything downstream reasons in probability space; `cp_white` is
//! kept for the log, not for decisions.

mod uci;

use anyhow::{bail, Context, Result};
use std::collections::BTreeMap;
use std::time::Duration;
use std::{path::Path, process::Stdio};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, Lines};
use tokio::process::{ChildStdin, ChildStdout};
use tokio::time::timeout;
use tokio::process::Command;
use tokio::sync::mpsc::{Receiver, Sender};
use tracing::info;

use crate::observation::{Clock, Line, Observation, Payload};
use crate::pgn::Position;
use shakmaty::fen::Fen;
use shakmaty::san::San;
use shakmaty::uci::UciMove;
use shakmaty::{CastlingMode, Chess, Position as _};
use uci::InfoLine;

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

    handshake(&mut stdin, &mut lines, multipv).await?;
    info!("engine ready: depth {depth}, MultiPV {multipv}, UCI_ShowWDL");

    // ---- per position ----
    while let Some(pos) = pos_rx.recv().await {
        stdin.write_all(uci_position(&pos).as_bytes()).await?;
        stdin
            .write_all(format!("go depth {depth}
").as_bytes())
            .await?;

        // Computed once, before the search: a record is sent the moment its depth finishes, and
        // replaying the game then would put a board walk inside the hot path.
        let white = uci::white_to_move(&pos);
        let board = board_for(&pos);
        let wanted = depths_of_interest(depth);

        let mut seen: BTreeMap<(u32, u32), InfoLine> = BTreeMap::new();
        let mut done: Vec<u32> = Vec::new();
        let mut deepest = 0u32;

        while let Some(line) = lines.next_line().await? {
            if line.starts_with("bestmove") {
                break;
            }
            let Some(info) = uci::parse_info(&line) else {
                continue;
            };

            // A line at a greater depth means every shallower one is final. Waiting for `bestmove`
            // instead — as this did — makes the cheap early depth arrive with the expensive one,
            // so it buys no earliness at all and there is no point computing it.
            if info.depth > deepest {
                for d in newly_final(&wanted, &done, info.depth) {
                    emit(d, &seen, &pos, board.as_ref(), white, multipv, &clock, &tx).await?;
                    done.push(d);
                }
                deepest = info.depth;
            }

            seen.insert((info.depth, info.multipv), info);
        }

        // The deepest depth only becomes final at `bestmove`, which is the next line anyway.
        for d in wanted.iter().copied() {
            if !done.contains(&d) {
                emit(d, &seen, &pos, board.as_ref(), white, multipv, &clock, &tx).await?;
            }
        }
    }
    Ok(())
}

/// Send one completed depth, if the engine produced it.
///
/// This awaits mid-search, so a full log channel stalls the engine rather than growing in memory.
/// That is the intended trade — see `CHANNEL_DEPTH` in `main.rs` — and it is what makes a slow sink
/// visible instead of silently buffered.
#[allow(clippy::too_many_arguments)]
async fn emit(
    d: u32,
    seen: &BTreeMap<(u32, u32), InfoLine>,
    pos: &Position,
    board: Option<&Chess>,
    white: bool,
    multipv: u32,
    clock: &Clock,
    tx: &Sender<Observation>,
) -> Result<()> {
    let Some(best) = seen.get(&(d, 1)) else {
        return Ok(());
    };
    let second = seen.get(&(d, 2));

    let mut payload_lines = Vec::new();
    for mp in 1..=multipv {
        let Some(info) = seen.get(&(d, mp)) else {
            continue;
        };
        let uci_move = info.pv_first.clone().unwrap_or_default();
        payload_lines.push(Line {
            san: board
                .and_then(|b| san_of(b, &uci_move))
                .unwrap_or_else(|| uci_move.clone()),
            uci: uci_move,
            cp: info.cp,
            mate_in: info.mate,
            wdl: info.wdl.unwrap_or([0, 0, 0]),
        });
    }

    tx.send(clock.stamp(Payload::Eval {
        game_id: pos.game_id.clone(),
        ply: pos.ply,
        depth: d,
        search_ms: best.time_ms,
        cp_white: uci::cp_white(best.cp, white),
        mate_in: uci::mate_white(best.mate, white),
        wdl_white: uci::wdl_white(best.wdl.unwrap_or([0, 0, 0]), white),
        only_move_gap_cp: uci::only_move_gap(best, second),
        lines: payload_lines,
    }))
    .await?;
    Ok(())
}

/// How long to wait for the engine to answer `uci` and `isready`.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);

/// Bring the engine up and set the options the analysis depends on.
///
/// Without `UCI_ShowWDL` every evaluation arrives with no probabilities, and the trigger — which
/// reasons only in probability space — would never fire.
async fn handshake(
    stdin: &mut ChildStdin,
    lines: &mut Lines<BufReader<ChildStdout>>,
    multipv: u32,
) -> Result<()> {
    stdin.write_all(b"uci
").await?;
    wait_for(lines, "uciok").await.context("engine did not answer `uci`")?;

    stdin
        .write_all(
            format!(
                "setoption name UCI_ShowWDL value true
                 setoption name MultiPV value {multipv}
                 setoption name Threads value 1
"
            )
            .as_bytes(),
        )
        .await?;

    stdin.write_all(b"isready
").await?;
    wait_for(lines, "readyok").await.context("engine did not answer `isready`")?;
    Ok(())
}

/// Read until a line starts with `token`, or give up.
///
/// The timeout is what turns a wrong --engine-path into a message rather than a hang.
async fn wait_for(lines: &mut Lines<BufReader<ChildStdout>>, token: &str) -> Result<()> {
    timeout(HANDSHAKE_TIMEOUT, async {
        while let Some(line) = lines.next_line().await? {
            if line.starts_with(token) {
                return Ok(());
            }
        }
        bail!("engine closed its output before sending `{token}`")
    })
    .await
    .with_context(|| format!("timed out after {HANDSHAKE_TIMEOUT:?} waiting for `{token}`"))?
}

/// Which depths of interest a line at `incoming` has just made final.
///
/// A depth is final once the engine has moved past it: Stockfish emits every `multipv` line for a
/// depth before starting the next, so a line at a greater depth means nothing more is coming for
/// the shallower ones. It never re-opens a depth it has left.
fn newly_final(wanted: &[u32], done: &[u32], incoming: u32) -> Vec<u32> {
    wanted
        .iter()
        .copied()
        .filter(|d| *d < incoming && !done.contains(d))
        .collect()
}

/// The depths to emit a record for: a cheap early one, then the decision depth.
fn depths_of_interest(target: u32) -> Vec<u32> {
    let mut out: Vec<u32> = vec![12, target].into_iter().filter(|d| *d <= target).collect();
    out.sort_unstable();
    out.dedup();
    out
}

/// The board this position leaves, for turning engine UCI back into SAN.
fn board_for(pos: &Position) -> Option<Chess> {
    let mut board: Chess = match &pos.start_fen {
        Some(fen) => fen
            .parse::<Fen>()
            .ok()?
            .into_position(CastlingMode::Standard)
            .ok()?,
        None => Chess::default(),
    };
    for m in &pos.moves {
        let mv = UciMove::from_ascii(m.as_bytes()).ok()?.to_move(&board).ok()?;
        board.play_unchecked(&mv);
    }
    Some(board)
}

/// SAN for an engine move, which the findability model reads to spot captures and checks.
fn san_of(board: &Chess, uci_move: &str) -> Option<String> {
    let mv = UciMove::from_ascii(uci_move.as_bytes())
        .ok()?
        .to_move(board)
        .ok()?;
    Some(San::from_move(board, &mv).to_string())
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
    fn a_deeper_line_finalises_the_shallower_depths() {
        // Depth 12 is done the moment depth 13 appears — roughly 580 ms before `bestmove`, which
        // is the whole reason for computing it.
        assert_eq!(newly_final(&[12, 16], &[], 13), vec![12]);
    }

    #[test]
    fn a_depth_is_not_final_while_the_engine_is_still_on_it() {
        assert_eq!(newly_final(&[12, 16], &[], 12), Vec::<u32>::new());
    }

    #[test]
    fn a_depth_is_never_emitted_twice() {
        assert_eq!(newly_final(&[12, 16], &[12], 14), Vec::<u32>::new());
    }

    #[test]
    fn jumping_several_depths_finalises_all_of_them() {
        // The engine can skip past a depth of interest between two lines we see.
        assert_eq!(newly_final(&[8, 12, 16], &[], 14), vec![8, 12]);
    }

    #[test]
    fn the_deepest_depth_is_never_finalised_by_a_line() {
        // `go depth 16` stops at 16, so nothing deeper ever arrives — it is flushed at `bestmove`.
        assert_eq!(newly_final(&[12, 16], &[12], 16), Vec::<u32>::new());
    }

    #[test]
    fn depths_of_interest_are_the_cheap_one_and_the_decision_one() {
        assert_eq!(depths_of_interest(16), vec![12, 16]);
        assert_eq!(depths_of_interest(12), vec![12], "no duplicate when they coincide");
        assert_eq!(depths_of_interest(8), vec![8], "a shallow target drops the early depth");
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
