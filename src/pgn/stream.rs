//! Turning a byte stream into a live, queryable set of games.

use std::collections::HashMap;

use super::moves::PlyInfo;
use super::Game;

/// The result tokens that end a game's movetext.
const RESULTS: [&str; 4] = ["*", "1-0", "0-1", "1/2-1/2"];

/// The id of a game: the last path segment of its `GameURL` header.
pub fn game_id(game: &Game<'_>) -> String {
    game.tag("GameURL")
        .and_then(|u| u.rsplit('/').next())
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| game.board_key())
}

/// A live view of every game seen on the stream, keyed by id.
#[derive(Debug, Default)]
pub struct GameStream {
    /// Bytes not yet forming a complete game.
    buf: Vec<u8>,
    games: HashMap<String, String>,
    emitted: HashMap<String, usize>,
    /// Ids in the order first seen, so iteration is stable rather than hash order.
    order: Vec<String>,
}

impl GameStream {
    pub fn new() -> Self {
        Self::default()
    }

    /// Feed the next chunk. Returns the ids of the games this chunk completed, in arrival order.
    pub fn push(&mut self, chunk: &[u8]) -> Vec<String> {
        self.buf.extend_from_slice(chunk);
        let mut updated = Vec::new();

        while let Some(end) = self.frame_end() {
            let frame: Vec<u8> = self.buf.drain(..end).collect();
            while self.buf.first().is_some_and(|b| *b == b'\n' || *b == b'\r') {
                self.buf.remove(0);
            }
            let Ok(text) = String::from_utf8(frame) else {
                continue;
            };
            let text = text.trim().to_string();
            if text.is_empty() {
                continue; // keep-alive
            }
            let Some(game) = super::split(&text).into_iter().next() else {
                continue;
            };
            let id = game_id(&game);
            if !self.games.contains_key(&id) {
                self.order.push(id.clone());
            }
            self.games.insert(id.clone(), text);
            updated.push(id);
        }
        updated
    }

    /// Byte offset just past the end of the first complete game in the buffer, if there is one.
    fn frame_end(&self) -> Option<usize> {
        let mut from = 0usize;
        while let Some(rel) = find(&self.buf[from..], b"\n\n") {
            let at = from + rel;
            if let Ok(head) = std::str::from_utf8(&self.buf[..at]) {
                let head = head.trim_end();
                if head.contains("[Event ") && RESULTS.iter().any(|r| head.ends_with(r)) {
                    return Some(at);
                }
            }
            from = at + 1;
        }
        None
    }

    /// The latest PGN for a game, if it has been seen.
    pub fn get(&self, id: &str) -> Option<&str> {
        self.games.get(id).map(String::as_str)
    }

    /// Ids in the order the games first appeared.
    pub fn ids(&self) -> &[String] {
        &self.order
    }

    pub fn len(&self) -> usize {
        self.games.len()
    }

    pub fn is_empty(&self) -> bool {
        self.games.is_empty()
    }

    /// Plies seen for the first time, per game this chunk updated.
    ///
    /// The stream re-sends a game's whole PGN on every update, so a watermark per board is what
    /// turns "here is the game again" into "here are the two new moves".
    pub fn new_plies(&mut self, chunk: &[u8]) -> Vec<(String, Vec<PlyInfo>)> {
        let ids = self.push(chunk);
        let mut out = Vec::new();
        let mut marks = Vec::new();
        for id in ids {
            let Some(text) = self.games.get(&id) else { continue };
            let Some(game) = super::split(text).into_iter().next() else {
                continue;
            };
            let all = super::moves::plies(&game);
            let seen = self.emitted.get(&id).copied().unwrap_or(0);
            if all.len() > seen {
                marks.push((id.clone(), all.len()));
                out.push((id, all[seen..].to_vec()));
            }
        }
        for (id, n) in marks {
            self.emitted.insert(id, n);
        }
        out
    }

    /// Bytes held back because they do not yet form a complete game. Useful when debugging a
    pub fn pending_bytes(&self) -> usize {
        self.buf.len()
    }
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

#[cfg(test)]
mod tests {
    use super::*;

    const URL: &str = "https://lichess.org/broadcast/indywidualne-mistrzostwa-polski-do-20-lat-open/runda-2/3XeWaGk9";

    fn game(id: &str, moves: &str, result: &str) -> String {
        format!(
            "[Event \"slesin\"]\n[Site \"Slesin\"]\n[GameURL \"{URL}/{id}\"]\n\
             [White \"A, A\"]\n[Black \"B, B\"]\n[Result \"{result}\"]\n\n{moves} {result}\n\n"
        )
    }

    #[test]
    fn one_game_in_one_chunk() {
        let mut s = GameStream::new();
        let ids = s.push(game("f1G6ANcY", "1. e4 e5", "*").as_bytes());
        assert_eq!(ids, vec!["f1G6ANcY"]);
        assert!(s.get("f1G6ANcY").unwrap().contains("1. e4 e5"));
    }

    #[test]
    fn the_id_comes_from_gameurl_not_site() {
        let mut s = GameStream::new();
        s.push(game("f1G6ANcY", "1. e4", "*").as_bytes());
        assert_eq!(s.ids(), ["f1G6ANcY"]);
    }

    #[test]
    fn the_blank_line_inside_a_game_is_not_a_boundary() {
        let mut s = GameStream::new();
        s.push(game("aaaaaaaa", "1. d4 d5", "*").as_bytes());
        assert_eq!(s.len(), 1);
        let pgn = s.get("aaaaaaaa").unwrap();
        assert!(pgn.contains("[White "), "headers kept");
        assert!(pgn.contains("1. d4 d5"), "moves kept");
    }

    #[test]
    fn two_games_in_one_chunk() {
        let mut s = GameStream::new();
        let both = format!("{}{}", game("aaa", "1. e4", "*"), game("bbb", "1. d4", "*"));
        assert_eq!(s.push(both.as_bytes()), vec!["aaa", "bbb"]);
        assert_eq!(s.len(), 2);
    }

    #[test]
    fn a_game_split_across_chunks_is_reassembled() {
        let mut s = GameStream::new();
        let text = game("f1G6ANcY", "1. e4 e5 2. Nf3 Nc6", "*");
        let bytes = text.as_bytes();
        let mut ids = Vec::new();
        for b in bytes.chunks(7) {
            ids.extend(s.push(b));
        }
        assert_eq!(ids, vec!["f1G6ANcY"]);
        assert!(s.get("f1G6ANcY").unwrap().contains("2. Nf3 Nc6"));
    }

    #[test]
    fn a_byte_at_a_time_still_works() {
        let mut s = GameStream::new();
        let text = game("zzz", "1. c4", "*");
        let mut ids = Vec::new();
        for b in text.as_bytes() {
            ids.extend(s.push(&[*b]));
        }
        assert_eq!(ids, vec!["zzz"]);
    }

    #[test]
    fn a_multibyte_character_split_across_chunks_survives() {
        let text = format!(
            "[Event \"e\"]\n[GameURL \"{URL}/acc\"]\n[White \"Krasniewski, Maciej\"]\n\
             [Black \"Żółć, Łukasz\"]\n[Result \"*\"]\n\n1. e4 *\n\n"
        );
        let bytes = text.as_bytes();
        let mut s = GameStream::new();
        let mut ids = Vec::new();
        for b in bytes.chunks(3) {
            ids.extend(s.push(b));
        }
        assert_eq!(ids, vec!["acc"]);
        assert!(s.get("acc").unwrap().contains("Żółć"));
    }

    #[test]
    fn an_update_replaces_the_previous_version() {
        let mut s = GameStream::new();
        s.push(game("abc", "1. e4", "*").as_bytes());
        s.push(game("abc", "1. e4 e5 2. Nf3", "*").as_bytes());
        assert_eq!(s.len(), 1, "same board, not a second entry");
        let pgn = s.get("abc").unwrap();
        assert!(pgn.contains("2. Nf3"));
        assert_eq!(s.ids().len(), 1);
    }

    #[test]
    fn a_finished_game_is_stored_with_its_result() {
        let mut s = GameStream::new();
        s.push(game("fin", "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#", "1-0").as_bytes());
        let g = super::super::split(s.get("fin").unwrap())[0];
        assert!(g.is_finished());
        assert_eq!(g.result(), Some("1-0"));
    }

    #[test]
    fn all_four_result_tokens_terminate_a_frame() {
        for r in ["*", "1-0", "0-1", "1/2-1/2"] {
            let mut s = GameStream::new();
            assert_eq!(
                s.push(game("x", "1. e4", r).as_bytes()).len(),
                1,
                "result {r}"
            );
        }
    }

    #[test]
    fn keep_alive_newlines_are_ignored() {
        let mut s = GameStream::new();
        assert!(s.push(b"\n\n").is_empty());
        assert!(s.push(b"\n\n\n").is_empty());
        assert_eq!(s.len(), 0);
    }

    #[test]
    fn an_incomplete_game_is_held_not_emitted() {
        let mut s = GameStream::new();
        let ids = s.push(b"[Event \"e\"]\n[Result \"*\"]\n\n1. e4");
        assert!(ids.is_empty(), "no result token yet, so no frame");
        assert_eq!(s.len(), 0);
        assert!(s.pending_bytes() > 0);
    }

    #[test]
    fn new_plies_returns_only_what_is_new() {
        let mut s = GameStream::new();
        let first = s.new_plies(game("g", "1. e4 e5", "*").as_bytes());
        assert_eq!(first.len(), 1);
        assert_eq!(first[0].1.len(), 2, "both plies are new the first time");

        let second = s.new_plies(game("g", "1. e4 e5 2. Nf3", "*").as_bytes());
        assert_eq!(second[0].1.len(), 1, "only the third ply");
        assert_eq!(second[0].1[0].san, "Nf3");
        assert_eq!(second[0].1[0].ply, 3);
    }

    #[test]
    fn a_resent_game_with_no_new_moves_emits_nothing() {
        // The stream re-sends a whole game on every update, including ones where nothing changed
        // on this board.
        let mut s = GameStream::new();
        s.new_plies(game("g", "1. e4 e5", "*").as_bytes());
        assert!(s.new_plies(game("g", "1. e4 e5", "*").as_bytes()).is_empty());
    }

    #[test]
    fn each_board_has_its_own_watermark() {
        let mut s = GameStream::new();
        s.new_plies(game("a", "1. e4 e5", "*").as_bytes());
        let b = s.new_plies(game("b", "1. d4", "*").as_bytes());
        assert_eq!(b.len(), 1);
        assert_eq!(b[0].0, "b");
        assert_eq!(b[0].1.len(), 1);
    }

    #[test]
    fn unknown_id_returns_none() {
        assert!(GameStream::new().get("nope").is_none());
    }

    #[test]
    fn ids_keep_their_first_seen_order() {
        let mut s = GameStream::new();
        s.push(game("first", "1. e4", "*").as_bytes());
        s.push(game("second", "1. d4", "*").as_bytes());
        s.push(game("first", "1. e4 e5", "*").as_bytes());
        assert_eq!(s.ids(), ["first", "second"]);
    }
}
