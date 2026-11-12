//! Producers.
//!
//! # The contract every source obeys
//!
//! A source is an `async fn` that takes a [`Clock`](crate::Clock), an
//! `mpsc::Sender<Observation>`, and whatever configuration it needs, and runs until it is done or
//! it fails. It sends records; it never writes to the log and never looks at the other sources.
//!
//! That is the whole seam. It buys three things:
//!
//! 1. **Replay is not a special case.** `replay` is just another source, so the consumer loop and
//!    the log format are exercised identically whether the data came from the network or a file.
//! 2. **Sources fail independently.** A source returning `Err` takes down that source, not the run.
//! 3. **The engine cannot stall market data.** The chess feed hands positions to the engine over a
//!    separate channel, so a slow search delays evaluations only — market events keep being
//!    recorded with honest timestamps. Since the deliverable *is* a timestamp difference, this is
//!    the one piece of the design that cannot be compromised for convenience.
//!
//! When a source has something to say about itself — starting, reconnecting, giving up — it sends a
//! [`Payload::Note`](crate::Payload::Note). Gaps in the data should always be explainable
//! afterwards without guessing.

pub mod engine;
pub mod lichess;
pub mod polymarket;
pub mod replay;
