//! Reconstructing when each move was played, from the clocks in the PGN.

/// The increment rule for a round.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimeControl {
    pub increment_secs: u32,
    /// The move number the increment starts at. 1 means from the first move.
    pub increment_from_move: u32,
}

impl Default for TimeControl {
    /// The 2026 World Championship control: 30 s per move from move 41.
    fn default() -> Self {
        Self {
            increment_secs: 30,
            increment_from_move: 41,
        }
    }
}

impl TimeControl {
    pub const NONE: Self = Self {
        increment_secs: 0,
        increment_from_move: 1,
    };

    fn increment_for(&self, move_no: u32) -> u32 {
        if move_no >= self.increment_from_move {
            self.increment_secs
        } else {
            0
        }
    }
}

/// One ply's clock reading, as it appears in the PGN.
#[derive(Debug, Clone, Copy)]
pub struct ClockReading {
    /// Move number, so 1 for both `1. e4` and `1... e5`.
    pub move_no: u32,
    pub white: bool,
    /// Seconds left on that player's clock after the move.
    pub clk_secs: u32,
}

/// Wall-clock milliseconds for each ply, from the round's start.
///
/// `elapsed += previous_clock - this_clock + increment`, per player.
///
/// The base time is **not** added when a player reaches the increment move: Lichess's `%clk`
/// already includes any bonus the control grants. Adding it again costs exactly one bonus per
/// player — an hour, for a championship control — and puts the end of the game well into the
/// future.
///
/// Spacing between moves is exact to the second. What is not known is the offset between the
/// scheduled start and the first move, so these are reconstructions, not measurements.
pub fn move_times_ms(
    readings: &[ClockReading],
    starts_at_ms: u64,
    tc: TimeControl,
    base_secs: u32,
) -> Vec<u64> {
    let mut prev = [base_secs as i64, base_secs as i64]; // [white, black]
    let mut elapsed: i64 = 0;
    let mut out = Vec::with_capacity(readings.len());

    for r in readings {
        let side = usize::from(!r.white);
        let consumed = prev[side] - r.clk_secs as i64 + tc.increment_for(r.move_no) as i64;
        prev[side] = r.clk_secs as i64;
        elapsed += consumed.max(0);
        out.push(starts_at_ms + (elapsed as u64) * 1000);
    }
    out
}

/// Parse `[TimeControl]` when a round provides one, e.g. `"600+5"` or `"40/7200:1800+30"`.
///
/// Many relayed rounds carry no such header, so this returns `None` rather than guessing and the
/// caller supplies the control instead.
pub fn parse_time_control(tag: &str) -> Option<TimeControl> {
    let last = tag.rsplit(':').next()?;
    let (left, inc) = last.split_once('+')?;
    let increment_secs = inc.trim().parse().ok()?;

    // "40/7200:1800+30": the increment begins after the first period's 40 moves.
    let increment_from_move = match tag.split(':').next().and_then(|p| p.split_once('/')) {
        Some((moves, _)) if tag.contains(':') => moves.trim().parse::<u32>().ok()? + 1,
        _ => {
            let _ = left;
            1
        }
    };
    Some(TimeControl {
        increment_secs,
        increment_from_move,
    })
}

/// Parse Lichess's free-text time control, from a broadcast's `tour.info.tc`.
///
/// Prose written by a tournament operator, not a format — `"90 min / 40 moves + 30 min + 30 sec /
/// move"`, `"60m+30s"`, `"13 minutes + 2 sec / move"`. So this reads it the way a person does:
/// take every `<number> <unit>` pair, and the **last** one measured in seconds is the increment,
/// because that is where an increment is always written.
///
/// Unlike PGN's `40/7200:1800+30`, where the increment belongs to the second period, prose like
/// `"+ 30 sec / move"` means every move from the first. Only an explicit `"starting 41"` or
/// `"from move 41"` moves it, and operators do write that when they mean it.
///
/// A **delay** is not an increment — the clock does not gain the time — so it reconstructs as no
/// increment at all rather than as a bonus that was never given.
pub fn parse_info_tc(tc: &str) -> Option<TimeControl> {
    let lower = tc.to_ascii_lowercase();
    let bytes = lower.as_bytes();

    let mut increment_secs: Option<u32> = None;
    let mut i = 0usize;
    while i < bytes.len() {
        if !bytes[i].is_ascii_digit() {
            i += 1;
            continue;
        }
        let start = i;
        while i < bytes.len() && bytes[i].is_ascii_digit() {
            i += 1;
        }
        let value: u32 = lower[start..i].parse().ok()?;
        let unit_start = i + lower[i..].find(|c: char| !c.is_whitespace()).unwrap_or(0);
        let unit: String = lower[unit_start..]
            .chars()
            .take_while(char::is_ascii_alphabetic)
            .collect();
        // "moves" before "min": both begin with m, and "40 moves" is not forty minutes.
        if unit.starts_with("mov") || unit.starts_with("min") || unit == "m" || unit.is_empty() {
            continue;
        }
        if unit.starts_with("sec") || unit == "s" {
            increment_secs = Some(value);
        }
    }

    let increment_secs = increment_secs?;
    if lower.contains("delay") && !lower.contains("increment") {
        return Some(TimeControl::NONE);
    }

    let increment_from_move = ["starting", "from move", "after move"]
        .iter()
        .find_map(|key| {
            let at = lower.find(key)? + key.len();
            let rest = lower[at..].trim_start();
            let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
            digits.parse::<u32>().ok()
        })
        .unwrap_or(1);

    Some(TimeControl {
        increment_secs,
        increment_from_move,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn r(move_no: u32, white: bool, clk_secs: u32) -> ClockReading {
        ClockReading {
            move_no,
            white,
            clk_secs,
        }
    }

    #[test]
    fn elapsed_is_the_sum_of_both_players_time() {
        // White uses 9s, black 11s.
        let rs = [r(1, true, 7191), r(1, false, 7189)];
        let t = move_times_ms(&rs, 1_000_000, TimeControl::NONE, 7200);
        assert_eq!(t[0], 1_000_000 + 9_000);
        assert_eq!(t[1], 1_000_000 + (9 + 11) * 1000);
    }

    #[test]
    fn each_player_has_their_own_clock() {
        // Black thinking does not consume white's time.
        let rs = [r(1, true, 7190), r(1, false, 7100), r(2, true, 7180)];
        let t = move_times_ms(&rs, 0, TimeControl::NONE, 7200);
        assert_eq!(t[2] - t[1], 10_000, "white's second move cost 10s");
    }

    #[test]
    fn the_increment_counts_as_time_spent() {
        // With a 30s increment the clock can rise; the move still took time.
        let rs = [r(41, true, 7210)];
        let tc = TimeControl {
            increment_secs: 30,
            increment_from_move: 41,
        };
        let t = move_times_ms(&rs, 0, tc, 7200);
        assert_eq!(t[0], 20_000, "clock +10 with a +30 increment means 20s used");
    }

    #[test]
    fn the_increment_does_not_apply_before_its_move() {
        let rs = [r(40, true, 7210)];
        let tc = TimeControl {
            increment_secs: 30,
            increment_from_move: 41,
        };
        assert_eq!(move_times_ms(&rs, 0, tc, 7200)[0], 0, "clamped, not negative");
    }

    #[test]
    fn a_clock_that_rises_never_rewinds_the_game() {
        // Guards against the bonus-already-included trap: time must not go backwards.
        let rs = [r(1, true, 7190), r(1, false, 7190), r(2, true, 9999)];
        let t = move_times_ms(&rs, 0, TimeControl::NONE, 7200);
        assert!(t[2] >= t[1]);
    }

    #[test]
    fn times_are_non_decreasing() {
        let rs = [
            r(1, true, 7191),
            r(1, false, 7189),
            r(2, true, 7187),
            r(2, false, 7071),
        ];
        let t = move_times_ms(&rs, 0, TimeControl::NONE, 7200);
        assert!(t.windows(2).all(|w| w[0] <= w[1]));
    }

    #[test]
    fn parses_a_simple_increment() {
        assert_eq!(
            parse_time_control("600+5"),
            Some(TimeControl {
                increment_secs: 5,
                increment_from_move: 1
            })
        );
    }

    #[test]
    fn parses_a_championship_control() {
        assert_eq!(
            parse_time_control("40/7200:1800+30"),
            Some(TimeControl {
                increment_secs: 30,
                increment_from_move: 41
            })
        );
    }

    #[test]
    fn a_control_without_an_increment_is_none() {
        assert_eq!(parse_time_control("40/7200"), None);
        assert_eq!(parse_time_control("-"), None);
    }

    /// Every one of these was read from a live Lichess broadcast's `tour.info.tc`.
    #[test]
    fn parses_the_formats_broadcasts_actually_use() {
        let cases = [
            ("90 min / 40 moves + 30 min + 30 sec / move", 30, 1),
            ("120 min / 40 moves + 30 min / rest + 30 sec / move starting 41", 30, 41),
            ("90 min + 30 sec / move", 30, 1),
            ("90/40 + 30 min + 30 sec/move", 30, 1),
            ("30 min + 10 sec / move", 10, 1),
            ("60m+30s", 30, 1),
            ("30 min + 3 sec / move", 3, 1),
            ("13 minutes + 2 sec / move", 2, 1),
            ("20 min + 10 sec / move", 10, 1),
        ];
        for (text, secs, from) in cases {
            assert_eq!(
                parse_info_tc(text),
                Some(TimeControl {
                    increment_secs: secs,
                    increment_from_move: from
                }),
                "{text}"
            );
        }
    }

    #[test]
    fn forty_moves_is_not_forty_minutes() {
        // Both begin with "m". Reading "40 moves" as minutes would be silent and harmless-looking.
        assert_eq!(parse_info_tc("90 min / 40 moves"), None, "no increment stated");
    }

    #[test]
    fn prose_increments_start_at_move_one() {
        // The difference from PGN notation: "40/7200:1800+30" gives the increment to the SECOND
        // period, while "+ 30 sec / move" means every move. Getting this backwards costs 30s per
        // move for the first forty moves of every reconstruction.
        assert_eq!(parse_info_tc("90 min + 30 sec / move").unwrap().increment_from_move, 1);
        assert_eq!(parse_time_control("40/5400:1800+30").unwrap().increment_from_move, 41);
    }

    #[test]
    fn an_explicit_starting_move_is_honoured() {
        for text in [
            "90 min + 30 sec / move starting 41",
            "90 min + 30 sec / move from move 41",
            "90 min + 30 sec / move, after move 40",
        ] {
            let from = parse_info_tc(text).unwrap().increment_from_move;
            assert!(from == 41 || from == 40, "{text} gave {from}");
        }
    }

    #[test]
    fn a_delay_is_not_an_increment() {
        // With a delay the clock never gains the time, so counting it as a bonus would invent
        // seconds that were never spent.
        assert_eq!(parse_info_tc("90 min + 30 sec delay"), Some(TimeControl::NONE));
    }

    #[test]
    fn text_with_no_time_control_in_it_is_none() {
        for text in ["", "Classical", "standard", "-", "rapid and blitz"] {
            assert_eq!(parse_info_tc(text), None, "{text}");
        }
    }

    #[test]
    fn default_is_the_championship_control() {
        assert_eq!(TimeControl::default().increment_from_move, 41);
        assert_eq!(TimeControl::default().increment_secs, 30);
    }
}
