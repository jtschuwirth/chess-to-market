//! SAN movetext to UCI moves, via a replayed board.

use anyhow::{Context, Result};
use shakmaty::fen::Fen;
use shakmaty::san::San;
use shakmaty::{CastlingMode, Chess, Position as _};

use super::{Game, Position};

/// The SAN tokens of a movetext, with everything that is not a move removed.
pub fn san_tokens(movetext: &str) -> Vec<&str> {
    let bytes = movetext.as_bytes();
    let mut out = Vec::new();
    let mut i = 0usize;
    let mut depth = 0usize;

    while i < bytes.len() {
        match bytes[i] {
            b'{' => {
                while i < bytes.len() && bytes[i] != b'}' {
                    i += 1;
                }
                i += 1;
            }
            b'(' => {
                depth += 1;
                i += 1;
            }
            b')' => {
                depth = depth.saturating_sub(1);
                i += 1;
            }
            b';' => {
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
            }
            c if c.is_ascii_whitespace() => i += 1,
            _ => {
                let start = i;
                while i < bytes.len()
                    && !bytes[i].is_ascii_whitespace()
                    && !matches!(bytes[i], b'(' | b')' | b'{' | b'}' | b';')
                {
                    i += 1;
                }
                if depth == 0 {
                    if let Some(tok) = clean_token(&movetext[start..i]) {
                        out.push(tok);
                    }
                }
            }
        }
    }
    out
}

/// Strip decoration from a token, or reject it if it is not a move.
fn clean_token(tok: &str) -> Option<&str> {
    let tok = tok.trim_end_matches(['!', '?', '+', '#']);
    if tok.is_empty() || tok.starts_with('$') {
        return None;
    }
    if matches!(tok, "1-0" | "0-1" | "1/2-1/2" | "*") {
        return None;
    }
    // Move numbers: "12." or "12..."
    if tok.trim_end_matches('.').chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    // "12.e4" — a number glued to the move
    let tok = match tok.find(|c: char| !c.is_ascii_digit() && c != '.') {
        Some(0) => tok,
        Some(n) if tok[..n].chars().all(|c| c.is_ascii_digit() || c == '.') => &tok[n..],
        _ => tok,
    };
    (!tok.is_empty()).then_some(tok)
}

/// The starting position, from a `[FEN]` header if the game has one.
fn start_fen(game: &Game<'_>) -> Option<String> {
    game.tag("FEN").map(str::to_string)
}

/// Replay a game's SAN into the UCI move list an engine can take.
///
/// Stops at the first move that will not play, keeping everything before it, so one malformed token
/// late in a game does not discard the whole board.
pub fn to_position(game: &Game<'_>, game_id: impl Into<String>) -> Result<Position> {
    let fen = start_fen(game);
    let mut board: Chess = match &fen {
        Some(f) => f
            .parse::<Fen>()
            .context("parsing the [FEN] header")?
            .into_position(CastlingMode::Standard)
            .context("the [FEN] header is not a legal position")?,
        None => Chess::default(),
    };

    let mut moves = Vec::new();
    for tok in san_tokens(game.movetext()) {
        let Ok(san) = San::from_ascii(tok.as_bytes()) else {
            break;
        };
        let Ok(mv) = san.to_move(&board) else {
            break;
        };
        moves.push(mv.to_uci(CastlingMode::Standard).to_string());
        board.play_unchecked(&mv);
    }

    Ok(Position {
        game_id: game_id.into(),
        ply: moves.len() as u32,
        start_fen: fen,
        moves,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pgn::split;

    fn game_of(movetext: &str) -> String {
        format!("[Event \"t\"]\n[Result \"*\"]\n\n{movetext}\n")
    }

    #[test]
    fn strips_move_numbers_and_result() {
        assert_eq!(san_tokens("1. e4 e5 2. Nf3 Nc6 *"), ["e4", "e5", "Nf3", "Nc6"]);
    }

    #[test]
    fn strips_clock_comments() {
        let m = "1. e4 { [%clk 1:59:51] } e5 { [%clk 1:59:49] } *";
        assert_eq!(san_tokens(m), ["e4", "e5"]);
    }

    #[test]
    fn strips_annotations_glued_to_a_move() {
        assert_eq!(san_tokens("55. Rf2?? Rxf2 1-0"), ["Rf2", "Rxf2"]);
    }

    #[test]
    fn strips_nags_and_variations() {
        assert_eq!(san_tokens("1. e4 $1 (1. d4 d5) 1... c5 *"), ["e4", "c5"]);
    }

    #[test]
    fn handles_a_number_glued_to_the_move() {
        assert_eq!(san_tokens("1.e4 e5 2.Nf3 *"), ["e4", "e5", "Nf3"]);
    }

    #[test]
    fn converts_san_to_uci() {
        let pgn = game_of("1. e4 e5 2. Nf3 Nc6 *");
        let g = split(&pgn)[0];
        let p = to_position(&g, "id").unwrap();
        assert_eq!(p.moves, ["e2e4", "e7e5", "g1f3", "b8c6"]);
        assert_eq!(p.ply, 4);
        assert!(p.start_fen.is_none());
    }

    #[test]
    fn disambiguates_which_knight_moved() {
        // The reason this needs a chess library at all: "Nf3" does not say which knight.
        let pgn = game_of("1. Nf3 Nf6 2. Nc3 Nc6 3. Nb1 *");
        let g = split(&pgn)[0];
        let p = to_position(&g, "id").unwrap();
        assert_eq!(p.moves, ["g1f3", "g8f6", "b1c3", "b8c6", "c3b1"]);
    }

    #[test]
    fn castling_becomes_a_king_move() {
        let pgn = game_of("1. e4 e5 2. Nf3 Nf6 3. Bc4 Bc5 4. O-O O-O *");
        let g = split(&pgn)[0];
        let p = to_position(&g, "id").unwrap();
        assert_eq!(p.moves.last().unwrap(), "e8g8");
    }

    #[test]
    fn promotion_carries_the_piece() {
        let fen = "8/P7/8/8/8/8/8/K6k w - - 0 1";
        let pgn = format!("[Event \"t\"]\n[SetUp \"1\"]\n[FEN \"{fen}\"]\n[Result \"*\"]\n\n1. a8=Q *\n");
        let g = split(&pgn)[0];
        let p = to_position(&g, "id").unwrap();
        assert_eq!(p.moves, ["a7a8q"]);
        assert_eq!(p.start_fen.as_deref(), Some(fen));
    }

    #[test]
    fn an_unstarted_game_yields_no_moves() {
        let pgn = game_of("*");
        let p = to_position(&split(&pgn)[0], "id").unwrap();
        assert!(p.moves.is_empty());
        assert_eq!(p.ply, 0);
    }

    #[test]
    fn an_illegal_move_keeps_everything_before_it() {
        // One bad token late in a game must not discard the board up to that point.
        let pgn = game_of("1. e4 e5 2. Qxf7 *");
        let p = to_position(&split(&pgn)[0], "id").unwrap();
        assert_eq!(p.moves, ["e2e4", "e7e5"]);
    }

    #[test]
    fn clocks_do_not_leak_into_the_moves() {
        let pgn = game_of("1. d4 { [%clk 1:59:40] } Nf6 { [%clk 1:59:38] } *");
        let p = to_position(&split(&pgn)[0], "id").unwrap();
        assert_eq!(p.moves, ["d2d4", "g8f6"]);
    }
}
