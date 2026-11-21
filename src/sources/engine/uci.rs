//! Parsing UCI `info` lines, and putting their numbers on white's side of the board.

use crate::pgn::Position;

/// One `info` line's numbers, still from the **side to move**'s perspective.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InfoLine {
    pub depth: u32,
    pub multipv: u32,
    pub cp: Option<i32>,
    pub mate: Option<i32>,
    pub wdl: Option<[u16; 3]>,
    pub time_ms: u64,
    pub pv_first: Option<String>,
}

fn after<'a>(tokens: &[&'a str], key: &str) -> Option<&'a str> {
    tokens.iter().position(|t| *t == key).and_then(|i| tokens.get(i + 1)).copied()
}

/// Parse an `info` line, or `None` if it is not one that carries a score.
///
/// Stockfish also emits `info depth 1 currmove ...` and `info string ...`; both are skipped.
pub fn parse_info(line: &str) -> Option<InfoLine> {
    if !line.starts_with("info ") {
        return None;
    }
    let t: Vec<&str> = line.split_whitespace().collect();

    let depth = after(&t, "depth")?.parse().ok()?;
    let multipv = after(&t, "multipv").and_then(|v| v.parse().ok()).unwrap_or(1);

    let score_at = t.iter().position(|x| *x == "score")?;
    let (cp, mate) = match t.get(score_at + 1)? {
        &"cp" => (t.get(score_at + 2)?.parse().ok(), None),
        &"mate" => (None, t.get(score_at + 2)?.parse().ok()),
        _ => return None,
    };

    let wdl = t.iter().position(|x| *x == "wdl").and_then(|i| {
        Some([
            t.get(i + 1)?.parse().ok()?,
            t.get(i + 2)?.parse().ok()?,
            t.get(i + 3)?.parse().ok()?,
        ])
    });

    Some(InfoLine {
        depth,
        multipv,
        cp,
        mate,
        wdl,
        time_ms: after(&t, "time").and_then(|v| v.parse().ok()).unwrap_or(0),
        pv_first: after(&t, "pv").map(str::to_string),
    })
}

/// Whose turn it is in this position.
///
/// Stockfish reports every score from the side to move, so this decides whether the numbers need
/// flipping. Getting it wrong makes the evaluation alternate sign every ply and turns every
/// computed swing into noise.
pub fn white_to_move(pos: &Position) -> bool {
    let base_is_white = match &pos.start_fen {
        Some(fen) => fen.split_whitespace().nth(1).map(|s| s == "w").unwrap_or(true),
        None => true,
    };
    if pos.moves.len() % 2 == 0 {
        base_is_white
    } else {
        !base_is_white
    }
}

/// Centipawns from white's perspective.
pub fn cp_white(cp: Option<i32>, white_to_move: bool) -> Option<i32> {
    cp.map(|v| if white_to_move { v } else { -v })
}

/// Mate distance from white's perspective. Positive means white mates.
pub fn mate_white(mate: Option<i32>, white_to_move: bool) -> Option<i32> {
    mate.map(|v| if white_to_move { v } else { -v })
}

/// `[win, draw, loss]` from white's perspective.
pub fn wdl_white(wdl: [u16; 3], white_to_move: bool) -> [u16; 3] {
    if white_to_move {
        wdl
    } else {
        [wdl[2], wdl[1], wdl[0]]
    }
}

/// Best line's score minus the second best, in centipawns.
///
/// Both are from the same perspective, so the gap needs no normalising. A large gap means the
/// evaluation rests on one move: an only-move, which a human may not find.
pub fn only_move_gap(best: &InfoLine, second: Option<&InfoLine>) -> Option<i32> {
    Some(best.cp? - second?.cp?)
}

#[cfg(test)]
mod tests {
    use super::*;

    const REAL: &str = "info depth 16 seldepth 28 multipv 1 score cp 28 wdl 54 940 6 \
nodes 713033 nps 941919 hashfull 224 tbhits 0 time 757 pv g1f3 b8c6 f1b5 g8f6";

    fn pos(start_fen: Option<&str>, n_moves: usize) -> Position {
        Position {
            game_id: "g".into(),
            ply: n_moves as u32,
            start_fen: start_fen.map(str::to_string),
            moves: (0..n_moves).map(|_| "e2e4".to_string()).collect(),
        }
    }

    #[test]
    fn parses_a_real_info_line() {
        let i = parse_info(REAL).unwrap();
        assert_eq!(i.depth, 16);
        assert_eq!(i.multipv, 1);
        assert_eq!(i.cp, Some(28));
        assert_eq!(i.mate, None);
        assert_eq!(i.wdl, Some([54, 940, 6]));
        assert_eq!(i.time_ms, 757);
        assert_eq!(i.pv_first.as_deref(), Some("g1f3"));
    }

    #[test]
    fn parses_a_mate_score_without_a_centipawn_value() {
        let l = "info depth 16 multipv 1 score mate -33 wdl 0 0 1000 time 12 pv e7g7";
        let i = parse_info(l).unwrap();
        assert_eq!(i.mate, Some(-33));
        assert_eq!(i.cp, None, "a mate is not a centipawn value");
    }

    #[test]
    fn multipv_defaults_to_one_when_absent() {
        let i = parse_info("info depth 8 score cp 10 time 3 pv e2e4").unwrap();
        assert_eq!(i.multipv, 1);
    }

    #[test]
    fn skips_lines_without_a_score() {
        assert!(parse_info("info depth 1 currmove e2e4 currmovenumber 1").is_none());
        assert!(parse_info("info string NNUE evaluation using nn-xyz.nnue").is_none());
        assert!(parse_info("bestmove g1f3 ponder b8c6").is_none());
    }

    #[test]
    fn missing_wdl_is_none_not_zero() {
        // UCI_ShowWDL off: absent must be distinguishable from a real 0/0/0.
        let i = parse_info("info depth 8 multipv 1 score cp 10 time 3 pv e2e4").unwrap();
        assert_eq!(i.wdl, None);
    }

    #[test]
    fn white_to_move_from_the_start_position() {
        assert!(white_to_move(&pos(None, 0)));
        assert!(!white_to_move(&pos(None, 1)));
        assert!(white_to_move(&pos(None, 2)));
    }

    #[test]
    fn white_to_move_respects_a_set_up_position() {
        let black_first = "8/8/8/4k3/8/8/4K3/8 b - - 0 1";
        assert!(!white_to_move(&pos(Some(black_first), 0)));
        assert!(white_to_move(&pos(Some(black_first), 1)));
    }

    #[test]
    fn scores_flip_when_black_is_to_move() {
        assert_eq!(cp_white(Some(28), true), Some(28));
        assert_eq!(cp_white(Some(28), false), Some(-28));
        assert_eq!(mate_white(Some(3), false), Some(-3));
    }

    #[test]
    fn wdl_swaps_win_and_loss_for_black() {
        assert_eq!(wdl_white([54, 940, 6], true), [54, 940, 6]);
        assert_eq!(wdl_white([54, 940, 6], false), [6, 940, 54]);
    }

    #[test]
    fn wdl_still_sums_to_a_thousand_after_flipping() {
        let f = wdl_white([120, 800, 80], false);
        assert_eq!(f.iter().map(|&x| x as u32).sum::<u32>(), 1000);
    }

    #[test]
    fn only_move_gap_is_the_difference_between_the_top_two() {
        let best = parse_info(REAL).unwrap();
        let second = parse_info(
            "info depth 16 multipv 2 score cp 14 wdl 32 958 10 time 757 pv f1c4",
        )
        .unwrap();
        assert_eq!(only_move_gap(&best, Some(&second)), Some(14));
    }

    #[test]
    fn only_move_gap_is_none_without_a_second_line() {
        let best = parse_info(REAL).unwrap();
        assert_eq!(only_move_gap(&best, None), None);
    }

    #[test]
    fn only_move_gap_is_none_when_a_score_is_a_mate() {
        // A mate and a centipawn value are not subtractable.
        let best = parse_info("info depth 16 multipv 1 score mate 2 time 5 pv e7g7").unwrap();
        let second = parse_info("info depth 16 multipv 2 score cp 900 time 5 pv d1d2").unwrap();
        assert_eq!(only_move_gap(&best, Some(&second)), None);
    }
}
