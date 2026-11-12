//! The Polymarket market-data WebSocket. **Ticket T-03 — not implemented.**
//!
//! The most async-interesting source in the program: a timer racing a stream, plus reconnection.
//!
//! # What you are building
//!
//! `wss://ws-subscriptions-clob.polymarket.com/ws/market` — **no authentication.**
//!
//! Subscribe:
//! ```json
//! {"assets_ids": ["<asset_id>"], "type": "market"}
//! ```
//!
//! Add or remove outcomes on an open connection:
//! ```json
//! {"assets_ids": ["<new_id>"], "operation": "subscribe"}
//! {"assets_ids": ["<old_id>"], "operation": "unsubscribe"}
//! ```
//!
//! Events are tagged by `event_type`: `book`, `price_change`, `last_trade_price`,
//! `tick_size_change`. Send each on as a [`Payload::Market`](crate::Payload::Market), keeping the
//! raw frame.
//!
//! **The heartbeat is mandatory:** send the *text frame* `PING` every 10 seconds — the literal
//! string, not a protocol ping frame — and the server replies `PONG`. Miss it and you are dropped.
//!
//! Put the heartbeat in the same `select!` as the read half rather than a spawned task. A spawned
//! task can outlive the connection it was pinging, and then you have a timer firing into a socket
//! that no longer exists.
//!
//! Reconnect with capped backoff plus jitter, re-subscribe on reconnect, and send a
//! [`Payload::Note`](crate::Payload::Note) every time you do — a gap in market data has to be
//! explainable afterwards, or the lag figures around it cannot be trusted.
//!
//! # Getting an asset id by hand
//!
//! ```bash
//! curl -s "https://gamma-api.polymarket.com/markets?slug=<slug>" | jq '.[0] | {question, clobTokenIds}'
//! ```
//!
//! `clobTokenIds` is a **double-encoded JSON string** — a JSON string whose contents are JSON.
//! Parse it twice.
//!
//! Read-only. No authenticated endpoint, no key, no order — ever (plan §4).
//!
//! ```bash
//! cargo add tokio-tungstenite --features rustls-tls-webpki-roots
//! ```

use anyhow::Result;
use tokio::sync::mpsc::Sender;

use crate::observation::{Clock, Observation};

pub async fn run(asset_ids: &[String], clock: Clock, tx: Sender<Observation>) -> Result<()> {
    let _ = tx
        .send(clock.note(format!(
            "polymarket: not implemented (T-03). asset_ids={}",
            asset_ids.join(",")
        )))
        .await;
    Ok(())
}
