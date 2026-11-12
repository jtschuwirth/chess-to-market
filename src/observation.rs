//! The one record type everything in the program produces.

use std::time::{Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

/// One record in the log.
///
/// # Why two clocks
///
/// `mono_ms` comes from a monotonic clock and is the **only** field you may subtract to get a
/// duration. `wall_unix_ms` comes from the system clock and exists only so records can be lined up
/// against the real game, a tournament broadcast or a screenshot.
///
/// The system clock can step — NTP corrections, DST, a VM resuming — and a measurement built on it
/// is quietly wrong in exactly the cases that matter. Since the whole deliverable here is a latency
/// figure, that distinction is the difference between a result and a bug.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Observation {
    /// Milliseconds since this process started. Monotonic. **Use this for durations.**
    pub mono_ms: u64,
    /// Milliseconds since the Unix epoch. Wall clock. For lining up with the outside world only.
    pub wall_unix_ms: u64,
    /// What happened.
    pub payload: Payload,
}

/// What a record is about. The `source` tag is what the Python analysis filters on.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "source", rename_all = "snake_case")]
pub enum Payload {
    /// A newly seen ply from the Lichess broadcast stream. Produced by `sources::lichess` (T-02).
    Ply {
        game_id: String,
        /// Half-moves since the start of the game. A *ply*, not a chess "move".
        ply: u32,
        san: String,
        /// Long algebraic, i.e. what the engine accepts.
        uci: String,
        fen: String,
    },
    /// A market event from the Polymarket WebSocket. Produced by `sources::polymarket` (T-03).
    Market {
        asset_id: String,
        /// Polymarket's own `event_type`: `book`, `price_change`, `last_trade_price`, …
        event_type: String,
        best_bid: Option<f64>,
        best_ask: Option<f64>,
        /// The untouched frame. Keeping it means a parsing mistake is recoverable after the fact,
        /// which matters when the data comes from a world championship game that happens once.
        raw: serde_json::Value,
    },
    /// An engine evaluation of a position. Produced by `sources::engine` (T-04).
    Eval {
        game_id: String,
        ply: u32,
        /// Centipawns, **normalised to white's perspective**. The engine reports from the side to
        /// move, so the sign flips every ply unless you normalise.
        eval_cp_white: Option<i32>,
        /// Set instead of `eval_cp_white` for a mate score. A mate is not a big centipawn number.
        mate_in: Option<i32>,
        /// Best line's eval minus second-best line's, from `MultiPV 2`. A large gap means an
        /// only-move: the evaluation only holds if a human finds one hard move.
        ///
        /// v1 **records this and acts on nothing**.
        only_move_gap_cp: Option<i32>,
        depth: u32,
    },
    /// Anything the program wants to say about itself: a source starting, dying, reconnecting.
    /// These are what make a gap in the data explainable afterwards.
    Note { message: String },
}

/// Hands out timestamps from a fixed start point.
///
/// Cheap to copy, so producers each take one rather than sharing state.
#[derive(Debug, Clone, Copy)]
pub struct Clock {
    start: Instant,
}

impl Clock {
    pub fn new() -> Self {
        Self {
            start: Instant::now(),
        }
    }

    /// Stamp a payload with both clocks, producing a complete record.
    pub fn stamp(&self, payload: Payload) -> Observation {
        Observation {
            mono_ms: self.start.elapsed().as_millis() as u64,
            wall_unix_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_millis() as u64)
                .unwrap_or(0),
            payload,
        }
    }

    /// Shorthand for a [`Payload::Note`].
    pub fn note(&self, message: impl Into<String>) -> Observation {
        self.stamp(Payload::Note {
            message: message.into(),
        })
    }
}

impl Default for Clock {
    fn default() -> Self {
        Self::new()
    }
}
