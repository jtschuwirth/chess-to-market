//! Positions for the games a chunk updated, ready for the engine.

use tracing::warn;

use super::moves::to_position;
use super::stream::GameStream;
use super::{split, Position};

impl GameStream {
    /// Feed a chunk; get back a [`Position`] for every game it completed.
    ///
    /// A game whose moves cannot be replayed is logged and skipped rather than failing the batch.
    pub fn push_positions(&mut self, chunk: &[u8]) -> Vec<Position> {
        let mut out = Vec::new();
        for id in self.push(chunk) {
            let Some(text) = self.get(&id) else { continue };
            let Some(game) = split(text).into_iter().next() else {
                continue;
            };
            match to_position(&game, &id) {
                Ok(p) => out.push(p),
                Err(e) => warn!("pgn: game {id} could not be replayed: {e:#}"),
            }
        }
        out
    }

    /// The current position of one game, if it has been seen.
    pub fn position(&self, id: &str) -> Option<Position> {
        let text = self.get(id)?;
        let game = split(text).into_iter().next()?;
        to_position(&game, id).ok()
    }

    /// Does this game have a result? `None` when the id is unknown.
    pub fn is_finished(&self, id: &str) -> Option<bool> {
        let text = self.get(id)?;
        let game = split(text).into_iter().next()?;
        Some(game.is_finished())
    }

    /// Like [`GameStream::push_positions`], but only for games still in play.
    ///
    /// A finished game keeps arriving on the stream — Lichess re-sends it with its result — and
    /// evaluating a decided game is engine time spent on a position nobody will trade.
    pub fn push_live_positions(&mut self, chunk: &[u8]) -> Vec<Position> {
        let mut out = Vec::new();
        for id in self.push(chunk) {
            let Some(text) = self.get(&id) else { continue };
            let Some(game) = split(text).into_iter().next() else {
                continue;
            };
            if game.is_finished() {
                continue;
            }
            match to_position(&game, &id) {
                Ok(p) => out.push(p),
                Err(e) => warn!("pgn: game {id} could not be replayed: {e:#}"),
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const URL: &str = "https://lichess.org/broadcast/t/r/3XeWaGk9";

    fn game(id: &str, moves: &str) -> String {
        format!(
            "[Event \"e\"]\n[GameURL \"{URL}/{id}\"]\n[White \"A\"]\n[Black \"B\"]\n\
             [Result \"*\"]\n\n{moves} *\n\n"
        )
    }

    #[test]
    fn a_chunk_yields_a_position_per_updated_game() {
        let mut s = GameStream::new();
        let both = format!("{}{}", game("aaa", "1. e4 e5"), game("bbb", "1. d4"));
        let ps = s.push_positions(both.as_bytes());
        assert_eq!(ps.len(), 2);
        assert_eq!(ps[0].game_id, "aaa");
        assert_eq!(ps[0].moves, ["e2e4", "e7e5"]);
        assert_eq!(ps[1].moves, ["d2d4"]);
    }

    #[test]
    fn an_update_yields_the_longer_move_list() {
        let mut s = GameStream::new();
        s.push_positions(game("abc", "1. e4").as_bytes());
        let ps = s.push_positions(game("abc", "1. e4 e5 2. Nf3").as_bytes());
        assert_eq!(ps.len(), 1);
        assert_eq!(ps[0].moves, ["e2e4", "e7e5", "g1f3"]);
        assert_eq!(ps[0].ply, 3);
    }

    #[test]
    fn a_partial_chunk_yields_nothing() {
        let mut s = GameStream::new();
        assert!(s.push_positions(b"[Event \"e\"]\n[Result \"*\"]\n\n1. e4").is_empty());
    }

    #[test]
    fn position_can_be_asked_for_by_id() {
        let mut s = GameStream::new();
        s.push_positions(game("zzz", "1. c4 c5").as_bytes());
        let p = s.position("zzz").unwrap();
        assert_eq!(p.moves, ["c2c4", "c7c5"]);
        assert!(s.position("nope").is_none());
    }

    fn finished_game(id: &str, moves: &str, result: &str) -> String {
        format!(
            "[Event \"e\"]\n[GameURL \"{URL}/{id}\"]\n[White \"A\"]\n[Black \"B\"]\n\
             [Result \"{result}\"]\n\n{moves} {result}\n\n"
        )
    }

    #[test]
    fn is_finished_reports_the_result_state() {
        let mut s = GameStream::new();
        s.push(game("live", "1. e4").as_bytes());
        s.push(finished_game("done", "1. e4 e5", "1-0").as_bytes());
        assert_eq!(s.is_finished("live"), Some(false));
        assert_eq!(s.is_finished("done"), Some(true));
        assert_eq!(s.is_finished("missing"), None);
    }

    #[test]
    fn live_positions_skip_decided_games() {
        let mut s = GameStream::new();
        let both = format!(
            "{}{}",
            game("live", "1. e4 e5"),
            finished_game("done", "1. d4 d5", "1-0")
        );
        let ps = s.push_live_positions(both.as_bytes());
        assert_eq!(ps.len(), 1, "the decided game is not worth evaluating");
        assert_eq!(ps[0].game_id, "live");
    }

    #[test]
    fn push_positions_still_returns_everything() {
        // The filtering belongs to the caller's choice of method, not to the framing.
        let mut s = GameStream::new();
        let both = format!(
            "{}{}",
            game("live", "1. e4"),
            finished_game("done", "1. d4", "0-1")
        );
        assert_eq!(s.push_positions(both.as_bytes()).len(), 2);
    }

    #[test]
    fn a_game_that_ends_stops_producing_live_positions() {
        let mut s = GameStream::new();
        assert_eq!(s.push_live_positions(game("g", "1. e4").as_bytes()).len(), 1);
        let done = finished_game("g", "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7", "1-0");
        assert!(s.push_live_positions(done.as_bytes()).is_empty());
        assert_eq!(s.is_finished("g"), Some(true));
    }

    #[test]
    fn clock_comments_do_not_reach_the_engine() {
        let mut s = GameStream::new();
        let g = game("clk", "1. e4 { [%clk 1:59:51] } e5 { [%clk 1:59:49] }");
        let ps = s.push_positions(g.as_bytes());
        assert_eq!(ps[0].moves, ["e2e4", "e7e5"]);
    }
}
