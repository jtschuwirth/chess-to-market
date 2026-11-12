//! The Lichess broadcast PGN feed. **Ticket T-02 — not implemented.**
//!
//! # What you are building
//!
//! `GET https://lichess.org/api/stream/broadcast/round/{round_id}.pgn` — no authentication. The
//! body never ends while the round is live: stream it with `reqwest`'s `bytes_stream()` rather than
//! awaiting the whole response, or the program will sit there forever having consumed nothing.
//!
//! The endpoint **re-sends game PGN as it updates**, so chunks are not one-ply-at-a-time. The real
//! work is deduplicating down to plies you have not seen before, which means holding per-game state
//! — and deciding who owns it.
//!
//! Per new ply, send a [`Payload::Ply`](crate::Payload::Ply) with the SAN, the UCI move and the
//! resulting FEN. SAN → UCI needs `shakmaty`, because `Nf3` does not say which knight moved;
//! resolving that requires full legal-move generation for the position.
//!
//! Positions also go to the engine, over a separate channel — see `sources::engine`.
//!
//! Reconnection is not this source's job; supervision lives in the consumer loop (T-05).
//!
//! ```bash
//! cargo add reqwest --no-default-features --features rustls-tls,stream
//! cargo add futures-util shakmaty pgn-reader
//! ```
//!
//! Adding `shakmaty` / `pgn-reader` makes this repo GPL-3.0. Add `LICENSE` in the same commit.

use anyhow::Result;
use tokio::sync::mpsc::Sender;

use crate::observation::{Clock, Observation};

pub async fn run(round_id: &str, clock: Clock, tx: Sender<Observation>) -> Result<()> {
    let _ = tx
        .send(clock.note(format!(
            "lichess: not implemented (T-02). round_id={round_id}"
        )))
        .await;
    Ok(())
}
