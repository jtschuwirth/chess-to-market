//! The one record type everything in the program produces.
//!
//! **The wire format is specified in `analysis/SCHEMA.md`** and the Python side parses exactly that.
//! Changing a field name here without changing it there breaks the analysis silently, so the two
//! are verified against each other: `python -m analysis validate <log>`.

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

/// One MultiPV candidate line for the position *after* the ply being evaluated.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Line {
    pub san: String,
    pub uci: String,
    pub cp: Option<i32>,
    pub mate_in: Option<i32>,
    /// `[win, draw, loss]` in per-mille, **from the side to move** — the perspective the
    /// findability model needs ("can the player who must move find this?").
    pub wdl: [u16; 3],
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
        /// **Reconstructed** wall-clock time of the move: `startsAt + Σ(prev_clk − clk + inc)`.
        ///
        /// Not when the relay delivered it. Arrival time would measure Lichess's own lag; this
        /// measures the market's, which is the entire point of the exercise.
        move_unix_ms: u64,
    },
    /// A market event from the Polymarket WebSocket. Produced by `sources::polymarket` (T-03).
    Market {
        asset_id: String,
        /// The operator's label for this outcome, from `--asset-id <leg>=<id>`.
        ///
        /// Required: nothing downstream can infer which outcome an `asset_id` is, and a market
        /// record without a leg is unusable for anything except counting.
        leg: String,
        /// Polymarket's own `event_type`: `book`, `price_change`, `last_trade_price`, …
        event_type: String,
        best_bid: Option<f64>,
        best_ask: Option<f64>,
        /// The untouched frame. Keeping it means a parsing mistake is recoverable after the fact,
        /// which matters when the data comes from a world championship game that happens once.
        raw: serde_json::Value,
    },
    /// An engine evaluation of a position. Produced by `sources::engine` (T-04).
    ///
    /// One record **per completed depth of interest** (12 and 16), not one at the end: a full
    /// four-line set lands at depth 12 in ~70 ms and at depth 16 in ~1.6 s.
    Eval {
        game_id: String,
        ply: u32,
        depth: u32,
        /// The engine's own cumulative search time to this depth.
        search_ms: u64,
        /// Centipawns, **normalised to white's perspective**. Kept for the log and for comparison —
        /// **not** the trigger. Decisions are made in probability space.
        cp_white: Option<i32>,
        /// Set instead of `cp_white` for a mate score. A mate is not a big centipawn number.
        mate_in: Option<i32>,
        /// `[win, draw, loss]` in per-mille, **normalised to white**, from `UCI_ShowWDL`.
        ///
        /// This is the field the whole analysis reasons in: it is already in the same units as a
        /// market price. A move from +9 to +15 pawns is a 6-pawn swing and ~0 probability, which is
        /// precisely the false signal centipawns manufacture.
        wdl_white: [u16; 3],
        /// Best line's eval minus second-best, from MultiPV. A large gap means an only-move: the
        /// evaluation only holds if a human finds one hard move.
        only_move_gap_cp: Option<i32>,
        /// MultiPV candidates for the position after this ply, best first. **MultiPV ≥ 4**, or the
        /// holding-move count saturates and the findability model cannot tell "two moves hold" from
        /// "every move holds".
        lines: Vec<Line>,
    },
    /// The decision stage's output (T-06). A pure function of the records above.
    Signal {
        game_id: String,
        ply: u32,
        /// `decisive` (level → winning), `flip` (winning → the other side) or `collapse`
        /// (winning → level). Nothing else is a trigger.
        kind: String,
        confidence: f32,
        /// Which terms fired, so a log line is self-explaining six months later.
        reason: String,
        state_before: String,
        state_after: String,
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

    /// Wall-clock milliseconds now. Also used to stamp reconstructed move times.
    pub fn wall_ms() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0)
    }

    /// Stamp a payload with both clocks, producing a complete record.
    pub fn stamp(&self, payload: Payload) -> Observation {
        Observation {
            mono_ms: self.start.elapsed().as_millis() as u64,
            wall_unix_ms: Self::wall_ms(),
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
