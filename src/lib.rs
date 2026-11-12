//! chess-to-market — measures how long a prediction market takes to price information a chess engine
//! already has.
//!
//! # The shape of this program
//!
//! Every feed is a **producer** that sends [`Observation`](observation::Observation) values into one
//! `tokio::sync::mpsc` channel. One **consumer** loop drains that channel into an append-only JSONL
//! log. That is the only seam in the program, and it is deliberately the only one:
//!
//! - live mode spawns the network/engine producers;
//! - replay mode spawns a producer that reads a recorded log;
//! - **the consumer cannot tell the difference**, which is why replay is a real test of the
//!   pipeline rather than a separate code path that drifts.
//!
//! Adding a feed means writing a producer. Nothing else in the program changes.

pub mod cli;
pub mod observation;
pub mod sink;
pub mod sources;

pub use observation::{Clock, Line, Observation, Payload};
