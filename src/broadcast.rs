//! A Lichess broadcast round's metadata, and the log file it belongs in.
//!
//! Fetched once, before anything is written. Two things need it:
//!
//! - the move-time reconstruction needs `startsAt` to anchor its clock arithmetic;
//! - the log path needs the tournament and round, so a rehearsal against yesterday's round cannot
//!   land in the file a live run will be read from.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

use crate::pgn::clocks::{parse_info_tc, TimeControl};

/// What a round says about itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoundMeta {
    pub round_id: String,
    pub round_slug: String,
    pub round_name: String,
    pub tour_id: String,
    pub tour_slug: String,
    pub tour_name: String,
    /// Scheduled start, Unix milliseconds. Anchors every reconstructed move time.
    pub starts_at_ms: u64,
    /// The operator's time control, verbatim, e.g. `"90 min / 40 moves + 30 min + 30 sec / move"`.
    pub tc: Option<String>,
}

#[derive(Deserialize)]
struct RoundResponse {
    round: RoundPart,
    tour: TourPart,
}

#[derive(Deserialize)]
struct RoundPart {
    id: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    slug: String,
    #[serde(rename = "startsAt")]
    starts_at: Option<u64>,
}

#[derive(Deserialize)]
struct TourPart {
    id: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    slug: String,
    #[serde(default)]
    info: TourInfo,
}

#[derive(Deserialize, Default)]
struct TourInfo {
    /// Free text, written by the tournament operator. `clocks::parse_info_tc` reads it.
    #[serde(default)]
    tc: Option<String>,
}

/// Keep what is safe in a path and recognisable to a human; drop the rest.
///
/// Lichess slugs are already lowercase ASCII with hyphens, so this normally changes nothing. It
/// exists for the day one is not: a slug carrying `..` or a separator would otherwise decide where
/// the program writes.
fn safe(segment: &str, fallback: &str) -> String {
    let cleaned: String = segment
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect();
    let trimmed = cleaned.trim_matches('-');
    if trimmed.is_empty() {
        return fallback.to_string();
    }
    trimmed.chars().take(80).collect()
}

impl RoundMeta {
    /// `<dir>/<tournament>/<round>.jsonl`.
    ///
    /// Keyed on the round, not the run: restarting the same round appends to the same file, which
    /// is what you want — it is one stream of play interrupted by an operator, and the analysis
    /// splits process runs on the clocks anyway.
    pub fn log_path(&self, dir: &Path) -> PathBuf {
        dir.join(safe(&self.tour_slug, &self.tour_id))
            .join(format!("{}.jsonl", safe(&self.round_slug, &self.round_id)))
    }

    /// The time control, when the operator wrote one this parses.
    ///
    /// Most relayed rounds carry no `[TimeControl]` PGN header, so without this the move-time
    /// reconstruction is guessing at the increment — and an increment is seconds per move, every
    /// move, which compounds across a game.
    pub fn time_control(&self) -> Option<TimeControl> {
        self.tc.as_deref().and_then(parse_info_tc)
    }

    /// One line naming this round, so a log says what it is without its filename.
    pub fn describe(&self) -> String {
        format!(
            "lichess: {} · {} ({}), startsAt {}, time control {}",
            self.tour_name,
            self.round_name,
            self.round_id,
            self.starts_at_ms,
            self.tc.as_deref().unwrap_or("(not stated)")
        )
    }
}

/// Fetch a round's metadata. Public endpoint, no authentication.
pub async fn round_meta(round_id: &str) -> Result<RoundMeta> {
    let url = format!("https://lichess.org/api/broadcast/-/-/{round_id}");
    let body = reqwest::get(&url)
        .await
        .with_context(|| format!("fetching {url}"))?
        .error_for_status()
        .with_context(|| format!("round {round_id} does not exist, or the id is wrong"))?
        .text()
        .await?;
    parse(&body, round_id)
}

fn parse(body: &str, round_id: &str) -> Result<RoundMeta> {
    let r: RoundResponse = serde_json::from_str(body).context("parsing round metadata")?;
    Ok(RoundMeta {
        starts_at_ms: r.round.starts_at.context("round metadata has no startsAt")?,
        round_slug: if r.round.slug.is_empty() {
            round_id.to_string()
        } else {
            r.round.slug
        },
        round_name: if r.round.name.is_empty() {
            round_id.to_string()
        } else {
            r.round.name
        },
        round_id: r.round.id,
        tour_slug: r.tour.slug,
        tour_name: r.tour.name,
        tour_id: r.tour.id,
        tc: r.tour.info.tc.filter(|s| !s.trim().is_empty()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Trimmed from the real response for round `yRwFuCyK`.
    const REAL: &str = r#"{
      "round": {"id":"yRwFuCyK","name":"Round 5","slug":"round-5","ongoing":true,
                "startsAt":1795295700000,"url":"https://lichess.org/broadcast/x/round-5/yRwFuCyK"},
      "tour": {"id":"wYvYFVtt","name":"FIDE World Junior Chess Championship 2026 | Open",
               "slug":"fide-world-junior-chess-championship-2026-open","tier":4,
               "info":{"format":"11-round Swiss","tc":"90 min / 40 moves + 30 min + 30 sec / move",
                       "fideTC":"standard"}},
      "games": []
    }"#;

    fn meta() -> RoundMeta {
        parse(REAL, "yRwFuCyK").unwrap()
    }

    #[test]
    fn parses_a_real_response() {
        let m = meta();
        assert_eq!(m.round_id, "yRwFuCyK");
        assert_eq!(m.round_slug, "round-5");
        assert_eq!(m.round_name, "Round 5");
        assert_eq!(m.tour_slug, "fide-world-junior-chess-championship-2026-open");
        assert_eq!(m.starts_at_ms, 1795295700000);
    }

    #[test]
    fn the_time_control_comes_from_the_metadata() {
        // This round's PGN has no [TimeControl] header, so without the metadata the increment was
        // a guess — and a wrong one: 30s from move 41 instead of from move 1.
        assert_eq!(
            meta().time_control(),
            Some(TimeControl {
                increment_secs: 30,
                increment_from_move: 1
            })
        );
    }

    #[test]
    fn a_round_without_a_stated_control_has_none() {
        let m = RoundMeta { tc: None, ..meta() };
        assert_eq!(m.time_control(), None);
    }

    #[test]
    fn describe_records_the_control_it_used() {
        // The log has to say what the reconstruction assumed, or its move times cannot be checked.
        assert!(meta().describe().contains("30 sec / move"));
    }

    #[test]
    fn the_path_is_one_folder_per_tournament() {
        let p = meta().log_path(Path::new("logs"));
        assert_eq!(
            p,
            Path::new("logs")
                .join("fide-world-junior-chess-championship-2026-open")
                .join("round-5.jsonl")
        );
    }

    #[test]
    fn two_rounds_of_one_tournament_share_a_folder_and_not_a_file() {
        // The point of the whole change: a rehearsal against an old round must not land in the file
        // the live run will be read from.
        let a = meta();
        let b = RoundMeta {
            round_slug: "round-11".into(),
            round_id: "abcd1234".into(),
            ..a.clone()
        };
        let (pa, pb) = (a.log_path(Path::new("logs")), b.log_path(Path::new("logs")));
        assert_eq!(pa.parent(), pb.parent());
        assert_ne!(pa, pb);
    }

    #[test]
    fn a_slug_cannot_decide_where_we_write() {
        let m = RoundMeta {
            tour_slug: "../../etc".into(),
            round_slug: "a/b".into(),
            ..meta()
        };
        let p = m.log_path(Path::new("logs"));
        assert_eq!(p, Path::new("logs").join("etc").join("a-b.jsonl"));
    }

    #[test]
    fn missing_slugs_fall_back_to_ids() {
        let m = RoundMeta {
            tour_slug: String::new(),
            round_slug: "   ".into(),
            ..meta()
        };
        assert_eq!(
            m.log_path(Path::new("logs")),
            Path::new("logs").join("wYvYFVtt").join("yRwFuCyK.jsonl")
        );
    }

    #[test]
    fn a_round_without_a_start_time_is_an_error() {
        // Without it every reconstructed move time would be anchored to the epoch.
        let body = r#"{"round":{"id":"x","slug":"s","name":"n"},"tour":{"id":"t","slug":"u","name":"v"}}"#;
        assert!(parse(body, "x").is_err());
    }

    #[test]
    fn describe_names_the_round_for_the_log() {
        let d = meta().describe();
        assert!(d.contains("FIDE World Junior Chess Championship 2026 | Open"));
        assert!(d.contains("Round 5"));
        assert!(d.contains("yRwFuCyK"));
    }
}
