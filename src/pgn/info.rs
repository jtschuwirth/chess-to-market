//! Who is playing, and how the game ended, from the PGN headers.

use super::stream::{game_id, GameStream};
use super::{split, Game};

/// A game's headers, as the `game` record in `analysis/SCHEMA.md` wants them.
///
/// Everything but the id is optional: a relay with thin headers should still produce a record.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GameInfo {
    pub game_id: String,
    pub white: Option<String>,
    pub black: Option<String>,
    pub white_elo: Option<u32>,
    pub black_elo: Option<u32>,
    pub white_title: Option<String>,
    pub black_title: Option<String>,
    /// The PGN result token: `*` while in play, else `1-0`, `0-1` or `1/2-1/2`.
    pub result: Option<String>,
    /// Board number within the round, from the `Round` header, e.g. `5.2`.
    pub board: Option<String>,
    pub event: Option<String>,
    /// The `GameURL` header verbatim. Never constructed: a wrong link is worse than none.
    pub url: Option<String>,
}

impl GameInfo {
    /// Has this game a result? A resignation is the usual way a decisive game ends, so the token
    /// is the only honest signal — the final position is not mate and the board says nothing.
    pub fn is_finished(&self) -> bool {
        matches!(self.result.as_deref(), Some("1-0" | "0-1" | "1/2-1/2"))
    }
}

fn tag(game: &Game<'_>, name: &str) -> Option<String> {
    game.tag(name)
        .map(str::trim)
        .filter(|v| !v.is_empty() && *v != "?")
        .map(str::to_string)
}

/// Read one game's headers.
pub fn info(game: &Game<'_>) -> GameInfo {
    GameInfo {
        game_id: game_id(game),
        white: tag(game, "White"),
        black: tag(game, "Black"),
        white_elo: tag(game, "WhiteElo").and_then(|v| v.parse().ok()),
        black_elo: tag(game, "BlackElo").and_then(|v| v.parse().ok()),
        white_title: tag(game, "WhiteTitle"),
        black_title: tag(game, "BlackTitle"),
        result: tag(game, "Result"),
        board: tag(game, "Round"),
        event: tag(game, "BroadcastName").or_else(|| tag(game, "Event")),
        url: tag(game, "GameURL"),
    }
}

impl GameStream {
    /// The headers of one game, if it has been seen.
    pub fn info(&self, id: &str) -> Option<GameInfo> {
        let game = split(self.get(id)?).into_iter().next()?;
        Some(info(&game))
    }

    /// Feed a chunk; get back the headers of every game whose headers are **new or changed**.
    ///
    /// The stream re-sends a game's whole PGN on every update, so emitting on arrival would repeat
    /// the same names on every move. What is worth a record is the first sighting and the moment a
    /// result lands — which is exactly a change in these fields.
    pub fn new_infos(&mut self, chunk: &[u8]) -> Vec<GameInfo> {
        let ids = self.push(chunk);
        let mut out = Vec::new();
        for id in ids {
            let Some(next) = self.info(&id) else { continue };
            if self.infos.get(&id) == Some(&next) {
                continue;
            }
            self.infos.insert(id, next.clone());
            out.push(next);
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const URL: &str = "https://lichess.org/broadcast/world-junior-2026/round-5/yRwFuCyK";

    fn pgn(id: &str, result: &str, moves: &str) -> String {
        format!(
            "[Event \"Round 5: A - B\"]\n[Round \"5.2\"]\n\
             [White \"Novozhilov, Semen\"]\n[Black \"Vaz, Ethan\"]\n\
             [Result \"{result}\"]\n[WhiteElo \"2472\"]\n[BlackElo \"2511\"]\n\
             [WhiteTitle \"GM\"]\n[BlackTitle \"IM\"]\n\
             [BroadcastName \"FIDE World Junior 2026 | Open\"]\n\
             [GameURL \"{URL}/{id}\"]\n\n{moves} {result}\n\n"
        )
    }

    #[test]
    fn reads_every_header_the_schema_wants() {
        let text = pgn("MUkIEXlH", "*", "1. e4");
        let i = info(&split(&text)[0]);
        assert_eq!(i.game_id, "MUkIEXlH");
        assert_eq!(i.white.as_deref(), Some("Novozhilov, Semen"));
        assert_eq!(i.black.as_deref(), Some("Vaz, Ethan"));
        assert_eq!(i.white_elo, Some(2472));
        assert_eq!(i.black_elo, Some(2511));
        assert_eq!(i.white_title.as_deref(), Some("GM"));
        assert_eq!(i.board.as_deref(), Some("5.2"));
        assert_eq!(i.event.as_deref(), Some("FIDE World Junior 2026 | Open"));
        assert_eq!(i.url.as_deref(), Some(format!("{URL}/MUkIEXlH").as_str()));
    }

    #[test]
    fn a_star_result_is_not_finished() {
        assert!(!info(&split(&pgn("a", "*", "1. e4"))[0]).is_finished());
    }

    #[test]
    fn every_decisive_token_counts_as_finished() {
        for r in ["1-0", "0-1", "1/2-1/2"] {
            assert!(info(&split(&pgn("a", r, "1. e4"))[0]).is_finished(), "{r}");
        }
    }

    #[test]
    fn missing_and_placeholder_headers_are_none() {
        // Lichess writes "?" for an unknown player; that is absence, not a name.
        let text = "[Event \"e\"]\n[White \"?\"]\n[Black \"\"]\n[Result \"*\"]\n\n1. e4 *\n\n";
        let i = info(&split(text)[0]);
        assert_eq!(i.white, None);
        assert_eq!(i.black, None);
        assert_eq!(i.url, None);
    }

    #[test]
    fn a_game_is_reported_once_until_something_changes() {
        let mut s = GameStream::new();
        assert_eq!(s.new_infos(pgn("g1", "*", "1. e4").as_bytes()).len(), 1);
        // The same game again, one move later: same headers, so nothing new to say.
        assert!(s.new_infos(pgn("g1", "*", "1. e4 e5").as_bytes()).is_empty());
    }

    #[test]
    fn a_result_landing_is_reported() {
        let mut s = GameStream::new();
        s.new_infos(pgn("g1", "*", "1. e4").as_bytes());
        let out = s.new_infos(pgn("g1", "1-0", "1. e4 e5").as_bytes());
        assert_eq!(out.len(), 1);
        assert!(out[0].is_finished());
        assert_eq!(out[0].result.as_deref(), Some("1-0"));
    }

    #[test]
    fn info_can_be_asked_for_by_id() {
        let mut s = GameStream::new();
        s.push(pgn("zzz", "*", "1. e4").as_bytes());
        assert_eq!(s.info("zzz").unwrap().white.as_deref(), Some("Novozhilov, Semen"));
        assert!(s.info("nope").is_none());
    }
}
