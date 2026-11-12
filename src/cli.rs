//! Command line surface.

use std::path::PathBuf;

use clap::Parser;

/// Parse `--asset-id <leg>=<id>`.
///
/// The leg label has to come from the operator: nothing downstream can infer which outcome an
/// asset id refers to, and the analysis refuses to use market records without one.
fn parse_leg_id(s: &str) -> Result<(String, String), String> {
    let (leg, id) = s
        .split_once('=')
        .ok_or_else(|| format!("expected <leg>=<id>, got `{s}`"))?;
    let (leg, id) = (leg.trim(), id.trim());
    if leg.is_empty() || id.is_empty() {
        return Err(format!("expected <leg>=<id>, got `{s}`"));
    }
    Ok((leg.to_string(), id.to_string()))
}

#[derive(Parser, Debug, Clone)]
#[command(
    name = "chess-to-market",
    about = "Measures how long a prediction market takes to price information a chess engine already has.",
    long_about = "Read-only. Places no orders, holds no keys, touches no funds."
)]
pub struct Cli {
    /// Replay a recorded log instead of connecting to anything. Needs no network and no engine.
    #[arg(long, value_name = "FILE")]
    pub replay: Option<PathBuf>,

    /// Lichess broadcast round id (8 characters). Live mode.
    #[arg(long, value_name = "ID")]
    pub round_id: Option<String>,

    /// Polymarket outcome, as `<leg>=<asset_id>`. Repeat once per outcome.
    ///
    /// Example: `--asset-id gukesh=7137… --asset-id draw=9922… --asset-id sindarov=1044…`
    ///
    /// Found by hand — see ticket T-03. This is the primary path, not a fallback.
    #[arg(long = "asset-id", value_name = "LEG=ID", value_parser = parse_leg_id)]
    pub asset_ids: Vec<(String, String)>,

    /// Path to the Stockfish binary. Not vendored; install it separately.
    #[arg(long, value_name = "PATH")]
    pub engine_path: Option<PathBuf>,

    /// Engine search depth. Fixed by choice: adaptive depth would make evaluations
    /// incomparable between plies, which breaks the measurement.
    ///
    /// Default 16 — measured at zero phantom swings and a 1.4 s p90, against depth 12's roughly
    /// one phantom per game (`journey/research/depth-choice.md`).
    #[arg(long, default_value_t = 16)]
    pub depth: u32,

    /// MultiPV width. **4 or more**, or the findability model's holding-move count saturates and
    /// cannot tell "two moves hold" from "every move holds".
    #[arg(long, default_value_t = 4)]
    pub multipv: u32,

    /// Where to append the JSONL log.
    #[arg(long, default_value = "logs/run.jsonl", value_name = "FILE")]
    pub out: PathBuf,
}

impl Cli {
    /// True when we are replaying rather than connecting to anything.
    pub fn is_replay(&self) -> bool {
        self.replay.is_some()
    }
}

#[cfg(test)]
mod tests {
    use super::parse_leg_id;

    #[test]
    fn parses_a_labelled_asset_id() {
        assert_eq!(
            parse_leg_id("gukesh=7137").unwrap(),
            ("gukesh".to_string(), "7137".to_string())
        );
    }

    #[test]
    fn trims_whitespace() {
        assert_eq!(
            parse_leg_id(" draw = 99 ").unwrap(),
            ("draw".to_string(), "99".to_string())
        );
    }

    #[test]
    fn rejects_a_bare_id() {
        // Easy mistake to make, and it would produce market records the analysis refuses to use.
        assert!(parse_leg_id("7137").is_err());
    }

    #[test]
    fn rejects_an_empty_side() {
        assert!(parse_leg_id("=7137").is_err());
        assert!(parse_leg_id("gukesh=").is_err());
    }
}
