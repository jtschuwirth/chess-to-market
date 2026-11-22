//! Splitting a broadcast round's PGN into games, and picking out the ones still being played.

pub mod clocks;
pub mod info;
pub mod moves;
pub mod stream;
mod stream_positions;

pub struct Position {
    pub game_id: String,
    pub ply: u32,
    pub start_fen: Option<String>,
    pub moves: Vec<String>,
}

/// One game inside a round's PGN. Borrows from the source text — no copying.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Game<'a> {
    /// The complete game, headers and movetext.
    pub text: &'a str,
}

impl<'a> Game<'a> {
    /// The value of a PGN header, without quotes: `tag("White")` for `[White "Carlsen, Magnus"]`.
    pub fn tag(&self, name: &str) -> Option<&'a str> {
        for line in self.text.lines() {
            let Some(rest) = line.trim_start().strip_prefix('[') else {
                continue;
            };
            let Some(rest) = rest.strip_prefix(name) else {
                continue;
            };
            if let Some(rest) = rest.strip_prefix(" \"") {
                return rest.split('"').next();
            }
        }
        None
    }

    pub fn white(&self) -> Option<&'a str> {
        self.tag("White")
    }

    pub fn black(&self) -> Option<&'a str> {
        self.tag("Black")
    }

    pub fn result(&self) -> Option<&'a str> {
        self.tag("Result")
    }

    /// The game's own id, when there is one.
    pub fn id(&self) -> Option<&'a str> {
        let site = self.tag("Site")?;
        if !site.starts_with("https://lichess.org/") || !site.contains("/broadcast/") {
            return None;
        }
        site.rsplit('/').next().filter(|s| !s.is_empty())
    }

    /// Something stable to key a board on across updates of the same round.
    pub fn board_key(&self) -> String {
        match self.id() {
            Some(id) => id.to_string(),
            None => format!(
                "{}|{}",
                self.white().unwrap_or("?"),
                self.black().unwrap_or("?")
            ),
        }
    }

    /// Everything after the header block.
    pub fn movetext(&self) -> &'a str {
        match self.text.find("\n\n") {
            Some(i) => self.text[i + 2..].trim(),
            None => "",
        }
    }

    /// Has anyone moved? A game that has not started has an empty movetext, or just the result.
    pub fn has_moves(&self) -> bool {
        let m = self.movetext();
        !m.is_empty() && m != "*" && !m.starts_with('*')
    }

    /// A result is recorded, so the game is over.
    pub fn is_finished(&self) -> bool {
        !matches!(self.result(), None | Some("*"))
    }

    /// Being played right now: no result yet, but moves have been made.
    pub fn is_in_progress(&self) -> bool {
        !self.is_finished() && self.has_moves()
    }

    /// Scheduled but not begun: no result and no moves.
    pub fn is_not_started(&self) -> bool {
        !self.is_finished() && !self.has_moves()
    }
}

/// Split a round's PGN into games.
pub fn split(pgn: &str) -> Vec<Game<'_>> {
    let mut starts: Vec<usize> = Vec::new();
    let bytes = pgn.as_bytes();
    let mut from = 0;
    while let Some(rel) = pgn[from..].find("[Event ") {
        let at = from + rel;
        if at == 0 || bytes[at - 1] == b'\n' {
            starts.push(at);
        }
        from = at + 7;
    }
    starts
        .iter()
        .enumerate()
        .map(|(i, &s)| {
            let end = starts.get(i + 1).copied().unwrap_or(pgn.len());
            Game {
                text: pgn[s..end].trim_end(),
            }
        })
        .collect()
}

/// The games actually being played, at most `limit` of them.
pub fn in_progress(pgn: &str, limit: usize) -> Vec<Game<'_>> {
    split(pgn)
        .into_iter()
        .filter(Game::is_in_progress)
        .take(limit)
        .collect()
}

/// The same PGN with fewer games: the first `limit` whose `Result` is `*`.
pub fn take_unfinished(pgn: &str, limit: usize) -> String {
    let games: Vec<&str> = split(pgn)
        .into_iter()
        .filter(|g| !g.is_finished())
        .take(limit)
        .map(|g| g.text)
        .collect();
    if games.is_empty() {
        return String::new();
    }
    let mut out = games.join("\n\n");
    out.push('\n');
    out
}

/// How a round stands. Cheap to compute and worth logging when it changes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct RoundStatus {
    pub total: usize,
    pub finished: usize,
    pub in_progress: usize,
    pub not_started: usize,
}

impl RoundStatus {
    /// Every game has a result. Note that an **empty** round is not complete — otherwise a failed
    pub fn is_complete(&self) -> bool {
        self.total > 0 && self.finished == self.total
    }
}

pub fn status(pgn: &str) -> RoundStatus {
    let games = split(pgn);
    RoundStatus {
        total: games.len(),
        finished: games.iter().filter(|g| g.is_finished()).count(),
        in_progress: games.iter().filter(|g| g.is_in_progress()).count(),
        not_started: games.iter().filter(|g| g.is_not_started()).count(),
    }
}

/// Exit code used when a round has no games left to follow.
pub const EXIT_NO_ACTIVE_GAMES: i32 = 0;

/// Is there anything left to follow in this round?
pub fn has_active(pgn: &str) -> bool {
    let s = status(pgn);
    s.total > 0 && s.finished < s.total
}

/// The unfinished games, or exit the process if there are none.
pub fn active_or_exit(pgn: &str, limit: usize) -> String {
    if !has_active(pgn) {
        let s = status(pgn);
        tracing::info!(
            "round has no games still in play ({} game(s), all finished) — exiting",
            s.total
        );
        std::process::exit(EXIT_NO_ACTIVE_GAMES);
    }
    take_unfinished(pgn, limit)
}

#[cfg(test)]
mod tests {
    use super::*;

    const ROUND: &str = r#"[Event "Finished game"]
[Site "https://lichess.org/broadcast/t/r/RoUnD123/game0001"]
[White "Winner, A"]
[Black "Loser, B"]
[Result "1-0"]

1. e4 { [%clk 1:59:51] } e5 { [%clk 1:59:49] } 2. Qh5 1-0

[Event "Live game"]
[Site "https://lichess.org/broadcast/t/r/RoUnD123/game0002"]
[White "Gukesh D"]
[Black "Sindarov, Javokhir"]
[Result "*"]

1. d4 { [%clk 1:59:40] } Nf6 { [%clk 1:59:38] } *

[Event "Not started"]
[Site "https://lichess.org/broadcast/t/r/RoUnD123/game0003"]
[White "Waiting, C"]
[Black "Waiting, D"]
[Result "*"]

*

[Event "Drawn game"]
[Site "https://lichess.org/broadcast/t/r/RoUnD123/game0004"]
[White "Drawer, E"]
[Black "Drawer, F"]
[Result "1/2-1/2"]

1. c4 1/2-1/2
"#;

    #[test]
    fn splits_every_game() {
        assert_eq!(split(ROUND).len(), 4);
    }

    #[test]
    fn reads_headers() {
        let g = split(ROUND)[1];
        assert_eq!(g.white(), Some("Gukesh D"));
        assert_eq!(g.black(), Some("Sindarov, Javokhir"));
        assert_eq!(g.result(), Some("*"));
    }

    #[test]
    fn extracts_the_game_id_from_a_lichess_site_url() {
        assert_eq!(split(ROUND)[1].id(), Some("game0002"));
    }

    #[test]
    fn a_venue_name_in_site_is_not_a_game_id() {
        let relayed = "[Event \"R2\"]
[Site \"Slesin\"]
[White \"Lasok, Marek\"]
[Black \"Gibala, Adam\"]
[Result \"*\"]

1. e4 *
";
        let g = split(relayed)[0];
        assert_eq!(g.id(), None);
        assert_eq!(g.board_key(), "Lasok, Marek|Gibala, Adam");
    }

    #[test]
    fn board_key_prefers_the_game_id_when_present() {
        assert_eq!(split(ROUND)[1].board_key(), "game0002");
    }

    #[test]
    fn distinguishes_the_three_states() {
        let g = split(ROUND);
        assert!(g[0].is_finished() && !g[0].is_in_progress());
        assert!(g[1].is_in_progress(), "has moves and no result");
        assert!(g[2].is_not_started(), "result is * but nobody has moved");
        assert!(g[3].is_finished(), "a draw is a result");
    }

    #[test]
    fn a_result_of_star_alone_is_not_in_progress() {
        let not_started = split(ROUND)[2];
        assert_eq!(not_started.result(), Some("*"));
        assert!(!not_started.is_in_progress());
    }

    #[test]
    fn in_progress_respects_the_limit() {
        assert_eq!(in_progress(ROUND, 5).len(), 1);
        assert_eq!(in_progress(ROUND, 0).len(), 0);
    }

    #[test]
    fn take_unfinished_returns_only_star_games() {
        let out = take_unfinished(ROUND, 10);
        assert!(!out.contains("Winner, A"), "1-0 game should be gone");
        assert!(!out.contains("Drawer, E"), "1/2-1/2 game should be gone");
        assert!(out.contains("Gukesh D"));
        assert!(out.contains("Waiting, C"));
    }

    #[test]
    fn take_unfinished_respects_the_limit() {
        assert_eq!(split(&take_unfinished(ROUND, 1)).len(), 1);
        assert_eq!(split(&take_unfinished(ROUND, 2)).len(), 2);
        assert_eq!(
            split(&take_unfinished(ROUND, 99)).len(),
            2,
            "only two are unfinished"
        );
    }

    #[test]
    fn take_unfinished_output_is_still_valid_pgn() {
        let out = take_unfinished(ROUND, 2);
        let games = split(&out);
        assert_eq!(games.len(), 2);
        assert_eq!(games[0].white(), Some("Gukesh D"));
        assert_eq!(games[0].result(), Some("*"));
        assert!(games[0].movetext().contains("%clk"), "clocks must survive");
    }

    #[test]
    fn take_unfinished_is_empty_when_the_round_is_over() {
        let done = ROUND.replace("[Result \"*\"]", "[Result \"1-0\"]");
        assert_eq!(take_unfinished(&done, 5), "");
    }

    #[test]
    fn has_active_is_true_while_a_game_lacks_a_result() {
        assert!(has_active(ROUND));
    }

    #[test]
    fn has_active_is_false_once_every_game_has_a_result() {
        let done = ROUND.replace("[Result \"*\"]", "[Result \"1-0\"]");
        assert!(!has_active(&done));
    }

    #[test]
    fn has_active_is_false_for_an_empty_pgn() {
        assert!(!has_active(""));
    }

    #[test]
    fn has_active_counts_an_unstarted_game() {
        let unstarted = "[Event \"R1\"]
[Result \"*\"]

*
";
        assert!(has_active(unstarted));
    }

    #[test]
    fn status_counts_each_state() {
        let s = status(ROUND);
        assert_eq!(
            (s.total, s.finished, s.in_progress, s.not_started),
            (4, 2, 1, 1)
        );
        assert!(!s.is_complete());
    }

    #[test]
    fn a_round_with_every_result_in_is_complete() {
        let done = ROUND.replace("[Result \"*\"]", "[Result \"1-0\"]");
        assert!(status(&done).is_complete());
    }

    #[test]
    fn an_empty_round_is_not_complete() {
        assert!(!status("").is_complete());
        assert_eq!(split("").len(), 0);
    }

    #[test]
    fn games_keep_their_clock_comments() {
        assert!(split(ROUND)[1].movetext().contains("%clk"));
    }

    #[test]
    fn an_event_tag_inside_a_comment_does_not_split_a_game() {
        let tricky = "[Event \"Real\"]\n[Result \"*\"]\n\n1. e4 { see [Event \"fake\"] } *\n";
        assert_eq!(split(tricky).len(), 1);
    }
}
