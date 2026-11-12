//! Command line surface.

use std::path::PathBuf;

use clap::Parser;

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

    /// Polymarket asset id, from a market's `clobTokenIds`. Repeat for several outcomes.
    ///
    /// Found by hand — see ticket T-03. This is the primary path, not a fallback.
    #[arg(long = "asset-id", value_name = "ID")]
    pub asset_ids: Vec<String>,

    /// Path to the Stockfish binary. Not vendored; install it separately.
    #[arg(long, value_name = "PATH")]
    pub engine_path: Option<PathBuf>,

    /// Engine search depth. Fixed by choice: adaptive depth would make evaluations
    /// incomparable between plies, which breaks the measurement.
    #[arg(long, default_value_t = 18)]
    pub depth: u32,

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
