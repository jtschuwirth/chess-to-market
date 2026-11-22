//! The Polymarket market-data WebSocket (T-03).
//!
//! `wss://ws-subscriptions-clob.polymarket.com/ws/market` — **no authentication**, no key, no
//! order, ever. This subscribes to a public market-data channel and writes what it hears.
//!
//! # Shape
//!
//! One `select!` races three things: the socket's read half, a 10-second heartbeat, and the
//! control channel that says which markets should be watched. The heartbeat is a **text frame
//! containing `PING`** — the literal word, not a protocol ping — and the server answers `PONG`.
//! Miss it and the connection is dropped.
//!
//! The heartbeat lives in the same `select!` rather than a spawned task, because a spawned timer
//! outlives the connection it was pinging and then fires into a socket that no longer exists.
//!
//! # Reconnection
//!
//! Capped exponential backoff with jitter, re-subscribing on every reconnect, and a
//! [`Payload::Note`](crate::Payload::Note) each time. A gap in market data has to be explainable
//! afterwards or the lag figures around it cannot be trusted.
//!
//! The desired set of markets can change while connected — see [`crate::control`]. Adding an
//! outcome sends `{"assets_ids": [...], "operation": "subscribe"}` on the open socket; removing
//! one sends `unsubscribe`. No reconnect is needed for either.

pub mod book;

use std::collections::HashMap;
use std::time::Duration;

use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use serde_json::json;
use tokio::sync::mpsc::{Receiver, Sender};
use tokio_tungstenite::tungstenite::Message;
use tracing::{debug, warn};

use crate::market::Market;
use crate::observation::{Clock, Observation, Payload};

const URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/market";

/// The server drops a connection that stops talking. Ten seconds is its documented interval.
const HEARTBEAT: Duration = Duration::from_secs(10);

/// Backoff bounds for reconnection.
const BACKOFF_MIN: Duration = Duration::from_secs(1);
const BACKOFF_MAX: Duration = Duration::from_secs(30);

/// Which outcome, of which game, an asset id belongs to.
#[derive(Debug, Clone, Default)]
pub struct Legs {
    by_asset: HashMap<String, (String, String)>,
}

impl Legs {
    pub fn of(markets: &[Market]) -> Self {
        let mut by_asset = HashMap::new();
        for m in markets {
            for (leg, asset_id) in &m.legs {
                by_asset.insert(asset_id.clone(), (m.game_id.clone(), leg.clone()));
            }
        }
        Self { by_asset }
    }

    pub fn get(&self, asset_id: &str) -> Option<(&str, &str)> {
        self.by_asset
            .get(asset_id)
            .map(|(g, l)| (g.as_str(), l.as_str()))
    }

    pub fn asset_ids(&self) -> Vec<String> {
        self.by_asset.keys().cloned().collect()
    }

    pub fn is_empty(&self) -> bool {
        self.by_asset.is_empty()
    }
}

/// What changed between two desired sets.
pub fn diff(before: &Legs, after: &Legs) -> (Vec<String>, Vec<String>) {
    let added = after
        .by_asset
        .keys()
        .filter(|k| !before.by_asset.contains_key(*k))
        .cloned()
        .collect();
    let removed = before
        .by_asset
        .keys()
        .filter(|k| !after.by_asset.contains_key(*k))
        .cloned()
        .collect();
    (added, removed)
}

/// Backoff for attempt `n`, with jitter so reconnecting clients do not synchronise.
pub fn backoff(attempt: u32) -> Duration {
    let base = BACKOFF_MIN.saturating_mul(1u32 << attempt.min(5));
    let capped = base.min(BACKOFF_MAX);
    // Deterministic jitter from the attempt number: no rng dependency for a cosmetic spread.
    let jitter = Duration::from_millis(u64::from(attempt.wrapping_mul(137) % 500));
    capped + jitter
}

/// Follow a set of markets, which may change while running.
///
/// `markets` is the set to start with; `changes` delivers replacements. When both are empty this
/// idles rather than connecting — there is nothing to subscribe to, and an open socket with no
/// subscriptions is a connection that exists only to be explained later.
pub async fn run(
    markets: &[Market],
    clock: Clock,
    tx: Sender<Observation>,
    mut changes: Receiver<Vec<Market>>,
) -> Result<()> {
    let mut legs = Legs::of(markets);
    let mut attempt = 0u32;

    loop {
        if legs.is_empty() {
            // Nothing to watch yet. Wait for the control channel rather than opening a socket.
            match changes.recv().await {
                Some(next) => {
                    legs = Legs::of(&next);
                    if !legs.is_empty() {
                        let _ = tx
                            .send(clock.note(format!(
                                "polymarket: watching {} outcome(s)",
                                legs.asset_ids().len()
                            )))
                            .await;
                    }
                    continue;
                }
                None => return Ok(()),
            }
        }

        match session(&mut legs, &clock, &tx, &mut changes).await {
            Ok(Exit::Finished) => return Ok(()),
            Ok(Exit::Idle) => {
                attempt = 0;
                continue;
            }
            Ok(Exit::Reconnect) => {
                attempt = attempt.saturating_add(1);
                let wait = backoff(attempt);
                let _ = tx
                    .send(clock.note(format!(
                        "polymarket: disconnected, reconnecting in {:.1}s (attempt {attempt})",
                        wait.as_secs_f32()
                    )))
                    .await;
                tokio::time::sleep(wait).await;
            }
            Err(e) => {
                attempt = attempt.saturating_add(1);
                let wait = backoff(attempt);
                let _ = tx
                    .send(clock.note(format!(
                        "polymarket: {e:#}; reconnecting in {:.1}s (attempt {attempt})",
                        wait.as_secs_f32()
                    )))
                    .await;
                tokio::time::sleep(wait).await;
            }
        }
    }
}

enum Exit {
    /// The control channel closed: the program is shutting down.
    Finished,
    /// The socket went away. The caller backs off and tries again.
    Reconnect,
    /// Every market was removed. Nothing is wrong, so no backoff and no reconnect — wait for the
    /// operator to add one.
    Idle,
}

/// One connection, from subscribe to disconnect.
async fn session(
    legs: &mut Legs,
    clock: &Clock,
    tx: &Sender<Observation>,
    changes: &mut Receiver<Vec<Market>>,
) -> Result<Exit> {
    let (ws, _) = tokio_tungstenite::connect_async(URL)
        .await
        .context("connecting to the Polymarket market-data socket")?;
    let (mut write, mut read) = ws.split();

    let ids = legs.asset_ids();
    write
        .send(Message::Text(
            json!({"assets_ids": ids, "type": "market"}).to_string().into(),
        ))
        .await
        .context("subscribing")?;
    let _ = tx
        .send(clock.note(format!("polymarket: connected, subscribed to {} outcome(s)", ids.len())))
        .await;

    let mut books: HashMap<String, book::Book> = HashMap::new();
    let mut beat = tokio::time::interval(HEARTBEAT);
    beat.tick().await; // the first tick is immediate

    loop {
        tokio::select! {
            // The heartbeat shares this select! so it cannot outlive the socket it pings.
            _ = beat.tick() => {
                if write.send(Message::Text("PING".into())).await.is_err() {
                    return Ok(Exit::Reconnect);
                }
            }

            // Markets added or removed while connected: reconcile without dropping the socket.
            next = changes.recv() => {
                let Some(next) = next else { return Ok(Exit::Finished) };
                let after = Legs::of(&next);
                let (added, removed) = diff(legs, &after);
                for (ids, operation) in [(added, "subscribe"), (removed, "unsubscribe")] {
                    if ids.is_empty() {
                        continue;
                    }
                    let msg = json!({"assets_ids": ids, "operation": operation}).to_string();
                    if write.send(Message::Text(msg.into())).await.is_err() {
                        *legs = after;
                        return Ok(Exit::Reconnect);
                    }
                    let _ = tx
                        .send(clock.note(format!(
                            "polymarket: {operation}d {} outcome(s)", ids.len()
                        )))
                        .await;
                    if operation == "unsubscribe" {
                        for id in &ids {
                            books.remove(id);
                        }
                    }
                }
                *legs = after;
                if legs.is_empty() {
                    let _ = tx
                        .send(clock.note("polymarket: no markets left, waiting"))
                        .await;
                    return Ok(Exit::Idle);
                }
            }

            frame = read.next() => {
                let Some(frame) = frame else { return Ok(Exit::Reconnect) };
                let text = match frame {
                    Ok(Message::Text(t)) => t.to_string(),
                    Ok(Message::Binary(b)) => String::from_utf8_lossy(&b).into_owned(),
                    Ok(Message::Close(_)) | Err(_) => return Ok(Exit::Reconnect),
                    Ok(_) => continue,
                };

                for ev in book::events(&text) {
                    let Some(asset_id) = book::asset_id(&ev) else { continue };
                    let Some((game_id, leg)) = legs.get(asset_id) else {
                        debug!("polymarket: frame for an asset we are not watching");
                        continue;
                    };
                    let (game_id, leg) = (game_id.to_string(), leg.to_string());
                    let asset_id = asset_id.to_string();

                    let b = books.entry(asset_id.clone()).or_default();
                    b.apply(&ev);
                    let (best_bid, best_ask) = (b.best_bid(), b.best_ask());

                    let record = clock.stamp(Payload::Market {
                        game_id,
                        asset_id,
                        leg,
                        event_type: book::event_type(&ev).to_string(),
                        best_bid,
                        best_ask,
                        raw: ev,
                    });
                    if tx.send(record).await.is_err() {
                        warn!("polymarket: log channel closed");
                        return Ok(Exit::Finished);
                    }
                }
            }
        }
    }
}

/// Describe a market set for a log note.
pub fn describe(markets: &[Market]) -> String {
    if markets.is_empty() {
        return "none".to_string();
    }
    markets
        .iter()
        .map(Market::describe)
        .collect::<Vec<_>>()
        .join("; ")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn market(game: &str, legs: &[(&str, &str)]) -> Market {
        Market {
            game_id: game.into(),
            legs: legs.iter().map(|(l, a)| (l.to_string(), a.to_string())).collect(),
        }
    }

    #[test]
    fn an_asset_id_maps_back_to_its_game_and_leg() {
        let legs = Legs::of(&[market("g1", &[("white", "a1"), ("draw", "a2")])]);
        assert_eq!(legs.get("a1"), Some(("g1", "white")));
        assert_eq!(legs.get("a2"), Some(("g1", "draw")));
        assert_eq!(legs.get("nope"), None);
    }

    #[test]
    fn several_games_can_be_watched_at_once() {
        // The socket is one connection for every market; only the mapping is per game.
        let legs = Legs::of(&[
            market("g1", &[("white", "a1")]),
            market("g2", &[("white", "b1")]),
        ]);
        assert_eq!(legs.get("b1"), Some(("g2", "white")));
        assert_eq!(legs.asset_ids().len(), 2);
    }

    #[test]
    fn a_diff_says_what_to_subscribe_and_unsubscribe() {
        let before = Legs::of(&[market("g1", &[("white", "a1"), ("draw", "a2")])]);
        let after = Legs::of(&[market("g1", &[("white", "a1"), ("black", "a3")])]);
        let (added, removed) = diff(&before, &after);
        assert_eq!(added, ["a3"]);
        assert_eq!(removed, ["a2"]);
    }

    #[test]
    fn an_unchanged_set_sends_nothing() {
        let legs = Legs::of(&[market("g1", &[("white", "a1")])]);
        let (added, removed) = diff(&legs, &legs.clone());
        assert!(added.is_empty() && removed.is_empty());
    }

    #[test]
    fn removing_every_market_is_all_removals() {
        let before = Legs::of(&[market("g1", &[("white", "a1")])]);
        let after = Legs::default();
        let (added, removed) = diff(&before, &after);
        assert!(added.is_empty());
        assert_eq!(removed, ["a1"]);
        assert!(after.is_empty());
    }

    #[test]
    fn backoff_grows_and_is_capped() {
        assert!(backoff(1) < backoff(3));
        assert!(backoff(10) <= BACKOFF_MAX + Duration::from_millis(500));
        assert!(backoff(1) >= BACKOFF_MIN);
    }

    #[test]
    fn backoff_is_jittered_so_clients_do_not_synchronise() {
        assert_ne!(backoff(2), backoff(3));
    }
}
