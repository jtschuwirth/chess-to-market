//! Markets added and removed while the program runs.
//!
//! The dashboard cannot usefully be the only place a market is chosen if choosing one means
//! stopping the tool: a round is five hours long and the market for a game appears on the day. So
//! the set of markets being watched is **desired state in a file**, which the API writes and this
//! module reads.
//!
//! # Why a file, and why desired state
//!
//! A file rather than a socket keeps the Rust standalone — it is the artifact, and it has to run
//! with nothing else alive. It also means a market chosen while the tool was down is still there
//! when it starts.
//!
//! **Desired state rather than commands** is the part that matters. `add this` / `remove that`
//! has to be delivered exactly once, and a missed message leaves the subscription permanently out
//! of step with what the operator asked for. A file that says what *should* be watched can be
//! re-read at any time and converges by itself — a restart, a missed poll and a late write all end
//! in the same place.
//!
//! # Format
//!
//! `<log>.markets.json`, beside the round's log:
//!
//! ```json
//! {"markets": [
//!   {"game_id": "MUkIEXlH", "enabled": true,
//!    "legs": [{"leg": "white", "asset_id": "7137…"}, {"leg": "draw", "asset_id": "9922…"}]}
//! ]}
//! ```
//!
//! `enabled: false` keeps an entry for the operator without watching it, which is how the UI
//! offers a market it has already resolved without re-pasting the link.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::Result;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc::Sender;
use tracing::warn;

use crate::market::Market;

/// How often the file is checked. The operator is a human clicking a button; a second is not felt,
/// and it costs one `stat` of one file.
const POLL: Duration = Duration::from_secs(1);

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct Leg {
    pub leg: String,
    pub asset_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Entry {
    pub game_id: String,
    #[serde(default = "yes")]
    pub enabled: bool,
    #[serde(default)]
    pub legs: Vec<Leg>,
    /// What the operator pasted, kept so the UI can show it again. Ignored here.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

fn yes() -> bool {
    true
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct Desired {
    #[serde(default)]
    pub markets: Vec<Entry>,
}

impl Desired {
    /// The markets to actually watch: enabled, and with at least one outcome.
    pub fn active(&self) -> Vec<Market> {
        self.markets
            .iter()
            .filter(|e| e.enabled && !e.legs.is_empty())
            .map(|e| Market {
                game_id: e.game_id.clone(),
                legs: e
                    .legs
                    .iter()
                    .map(|l| (l.leg.clone(), l.asset_id.clone()))
                    .collect(),
            })
            .collect()
    }
}

/// Where the control file sits for a given log.
///
/// Beside the log, named after it, so one round's markets travel with that round's data and two
/// rounds watched at once do not share a file.
pub fn path_for(log: &Path) -> PathBuf {
    let mut name = log.file_stem().unwrap_or_default().to_os_string();
    name.push(".markets.json");
    log.with_file_name(name)
}

/// Read the desired state. A missing file is "nothing wanted", not an error.
pub fn read(path: &Path) -> Result<Desired> {
    match std::fs::read_to_string(path) {
        Ok(text) => Ok(parse(&text)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Desired::default()),
        Err(e) => Err(e.into()),
    }
}

/// Parse, tolerantly: a half-written file is a file we will read again in a second.
///
/// The writer is another process, and nothing coordinates the two. Treating a torn read as empty
/// would unsubscribe everything for a second; treating it as "no change" is both quieter and more
/// likely to be right.
pub fn parse(text: &str) -> Desired {
    serde_json::from_str(text).unwrap_or_default()
}

/// Write the starting markets, if nobody has written any yet.
///
/// Without this, `--market` on the command line and the control file fight: the file is desired
/// state, and an absent file says "nothing wanted", so the first poll would unsubscribe whatever
/// the flags asked for. Seeding makes the flags the *initial* desired state and leaves exactly one
/// source of truth from then on — which also means the dashboard can disable a market that was
/// started from the command line.
///
/// An existing file wins and is left alone: it is what the operator last chose.
pub fn seed(path: &Path, markets: &[Market]) -> Result<bool> {
    if markets.is_empty() || path.exists() {
        return Ok(false);
    }
    let desired = Desired {
        markets: markets
            .iter()
            .map(|m| Entry {
                game_id: m.game_id.clone(),
                enabled: true,
                legs: m
                    .legs
                    .iter()
                    .map(|(leg, asset_id)| Leg {
                        leg: leg.clone(),
                        asset_id: asset_id.clone(),
                    })
                    .collect(),
                source: Some("--market".to_string()),
            })
            .collect(),
    };
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(path, serde_json::to_string_pretty(&desired)?)?;
    Ok(true)
}

/// Watch the file and send the active market set whenever it changes.
///
/// Sends the current state once at startup, so a market chosen while the tool was down is picked
/// up rather than waiting for the next edit.
pub async fn watch(path: PathBuf, tx: Sender<Vec<Market>>) -> Result<()> {
    let mut last: Option<Vec<Market>> = None;
    loop {
        let desired = match read(&path) {
            Ok(d) => d,
            Err(e) => {
                warn!("control: {}: {e:#}", path.display());
                Desired::default()
            }
        };
        let active = desired.active();
        if last.as_ref() != Some(&active) {
            if tx.send(active.clone()).await.is_err() {
                return Ok(());
            }
            last = Some(active);
        }
        tokio::time::sleep(POLL).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"{"markets":[
        {"game_id":"g1","enabled":true,"legs":[
            {"leg":"white","asset_id":"a1"},{"leg":"draw","asset_id":"a2"}]},
        {"game_id":"g2","enabled":false,"legs":[{"leg":"white","asset_id":"b1"}]}
    ]}"#;

    #[test]
    fn only_enabled_markets_are_watched() {
        let active = parse(SAMPLE).active();
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].game_id, "g1");
        assert_eq!(active[0].asset_ids(), ["a1", "a2"]);
    }

    #[test]
    fn disabling_keeps_the_entry_but_stops_the_subscription() {
        // The UI offers a market it already resolved without making you paste the link again.
        let d = parse(SAMPLE);
        assert_eq!(d.markets.len(), 2);
        assert!(!d.markets[1].enabled);
    }

    #[test]
    fn an_entry_with_no_outcomes_is_not_watched() {
        // Half-filled by the UI: resolved the link, not yet mapped the legs.
        let d = parse(r#"{"markets":[{"game_id":"g","enabled":true,"legs":[]}]}"#);
        assert!(d.active().is_empty());
    }

    #[test]
    fn enabled_defaults_to_true() {
        let d = parse(r#"{"markets":[{"game_id":"g","legs":[{"leg":"white","asset_id":"a"}]}]}"#);
        assert_eq!(d.active().len(), 1);
    }

    #[test]
    fn a_torn_or_empty_file_means_no_change_not_no_markets() {
        // The writer is another process and nothing coordinates the two. Reading a half-written
        // file as "nothing wanted" would unsubscribe everything for a second.
        assert_eq!(parse("{\"markets\":[{\"game_"), Desired::default());
        assert_eq!(parse(""), Desired::default());
    }

    #[test]
    fn unknown_fields_are_ignored() {
        // The API may record things for its own use; the Rust must not break on them.
        let d = parse(
            r#"{"markets":[{"game_id":"g","legs":[{"leg":"white","asset_id":"a"}],
                "source":"https://polymarket.com/event/x","note":"whatever"}],"version":2}"#,
        );
        assert_eq!(d.active().len(), 1);
        assert_eq!(d.markets[0].source.as_deref(), Some("https://polymarket.com/event/x"));
    }

    #[test]
    fn a_missing_file_is_nothing_wanted() {
        let d = read(Path::new("does/not/exist.markets.json")).unwrap();
        assert!(d.active().is_empty());
    }

    #[test]
    fn the_control_file_sits_beside_its_log() {
        assert_eq!(
            path_for(Path::new("logs/world-junior/round-5.jsonl")),
            Path::new("logs/world-junior/round-5.markets.json")
        );
    }

    #[test]
    fn two_rounds_do_not_share_a_control_file() {
        assert_ne!(
            path_for(Path::new("logs/t/round-5.jsonl")),
            path_for(Path::new("logs/t/round-6.jsonl"))
        );
    }

    #[test]
    fn round_trips_through_json() {
        let d = parse(SAMPLE);
        assert_eq!(parse(&serde_json::to_string(&d).unwrap()), d);
    }
}

#[cfg(test)]
mod seed_tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("c2m-{}-{name}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("round-1.markets.json")
    }

    fn market() -> Market {
        Market {
            game_id: "g1".into(),
            legs: vec![("white".into(), "a1".into())],
        }
    }

    #[test]
    fn flags_become_the_first_desired_state() {
        // Without this the first poll of an absent file unsubscribes what --market asked for.
        let p = tmp("seed");
        let _ = std::fs::remove_file(&p);
        assert!(seed(&p, std::slice::from_ref(&market())).unwrap());
        assert_eq!(read(&p).unwrap().active(), vec![market()]);
    }

    #[test]
    fn an_existing_file_is_what_the_operator_last_chose() {
        let p = tmp("existing");
        std::fs::write(&p, r#"{"markets":[]}"#).unwrap();
        assert!(!seed(&p, std::slice::from_ref(&market())).unwrap());
        assert!(read(&p).unwrap().active().is_empty(), "the file wins");
    }

    #[test]
    fn no_flags_writes_no_file() {
        let p = tmp("empty");
        let _ = std::fs::remove_file(&p);
        assert!(!seed(&p, &[]).unwrap());
        assert!(!p.exists());
    }

    #[test]
    fn a_seeded_market_can_then_be_disabled() {
        // The point of seeding rather than merging: one source of truth, so the dashboard can
        // turn off a market that came from the command line.
        let p = tmp("disable");
        let _ = std::fs::remove_file(&p);
        seed(&p, std::slice::from_ref(&market())).unwrap();
        let mut d = read(&p).unwrap();
        d.markets[0].enabled = false;
        std::fs::write(&p, serde_json::to_string(&d).unwrap()).unwrap();
        assert!(read(&p).unwrap().active().is_empty());
    }
}
