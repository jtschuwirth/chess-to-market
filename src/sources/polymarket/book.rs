//! A local order book per outcome, and the frames that update it.
//!
//! The analysis wants the **touch** — best bid and best ask — at every event. The feed does not
//! send it: it sends a full book once, then deltas. So the touch has to be maintained here.
//!
//! Pure and synchronous on purpose. The socket loop is hard to test; this is where the logic that
//! can be wrong lives, and it can be tested with a string.

use std::collections::BTreeMap;

use serde_json::Value;

/// Prices are kept as integer ten-thousandths so they can be ordered and compared exactly.
/// Polymarket's tick is 0.01 or 0.001, so this has a digit to spare and no float keys.
type Tick = u32;

fn tick(price: f64) -> Tick {
    (price * 10_000.0).round().max(0.0) as Tick
}

fn price(t: Tick) -> f64 {
    (t as f64) / 10_000.0
}

fn as_f64(v: &Value) -> Option<f64> {
    v.as_f64().or_else(|| v.as_str()?.parse().ok())
}

/// One outcome's resting orders.
#[derive(Debug, Default, Clone)]
pub struct Book {
    bids: BTreeMap<Tick, f64>,
    asks: BTreeMap<Tick, f64>,
}

impl Book {
    /// Set the size resting at a price. **Size zero removes the level** — that is how the feed
    /// says a price is gone, and keeping it would leave a touch that no longer exists.
    pub fn set(&mut self, buy: bool, price_f: f64, size: f64) {
        let side = if buy { &mut self.bids } else { &mut self.asks };
        if size <= 0.0 {
            side.remove(&tick(price_f));
        } else {
            side.insert(tick(price_f), size);
        }
    }

    pub fn clear(&mut self) {
        self.bids.clear();
        self.asks.clear();
    }

    /// Highest price anyone will pay.
    pub fn best_bid(&self) -> Option<f64> {
        self.bids.keys().next_back().copied().map(price)
    }

    /// Lowest price anyone will sell at.
    pub fn best_ask(&self) -> Option<f64> {
        self.asks.keys().next().copied().map(price)
    }

    pub fn is_empty(&self) -> bool {
        self.bids.is_empty() && self.asks.is_empty()
    }

    /// Apply one event. Returns false if the frame was not about this book.
    pub fn apply(&mut self, ev: &Value) -> bool {
        match ev.get("event_type").and_then(Value::as_str) {
            Some("book") => {
                // A snapshot replaces what we had: anything missing from it is gone.
                self.clear();
                for (key, buy) in [("bids", true), ("buys", true), ("asks", false), ("sells", false)]
                {
                    for level in ev.get(key).and_then(Value::as_array).into_iter().flatten() {
                        let (Some(p), Some(s)) = (
                            level.get("price").and_then(as_f64),
                            level.get("size").and_then(as_f64),
                        ) else {
                            continue;
                        };
                        self.set(buy, p, s);
                    }
                }
                true
            }
            Some("price_change") => {
                // Either a `changes` array or a single change inline, depending on the feed's age.
                let changes: Vec<&Value> = match ev.get("changes").and_then(Value::as_array) {
                    Some(list) => list.iter().collect(),
                    None => vec![ev],
                };
                let mut touched = false;
                for c in changes {
                    let (Some(p), Some(s)) =
                        (c.get("price").and_then(as_f64), c.get("size").and_then(as_f64))
                    else {
                        continue;
                    };
                    let buy = matches!(
                        c.get("side").and_then(Value::as_str),
                        Some("BUY") | Some("buy")
                    );
                    self.set(buy, p, s);
                    touched = true;
                }
                touched
            }
            // A trade print does not change what is resting.
            _ => false,
        }
    }
}

/// The asset id a frame is about.
pub fn asset_id(ev: &Value) -> Option<&str> {
    ev.get("asset_id")
        .or_else(|| ev.get("assetId"))
        .and_then(Value::as_str)
}

pub fn event_type(ev: &Value) -> &str {
    ev.get("event_type").and_then(Value::as_str).unwrap_or("unknown")
}

/// Split an incoming text frame into the events it carries.
///
/// The feed sends either one object or an array of them, and `PONG` as a bare word.
pub fn events(text: &str) -> Vec<Value> {
    let trimmed = text.trim();
    if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("pong") {
        return Vec::new();
    }
    match serde_json::from_str::<Value>(trimmed) {
        Ok(Value::Array(items)) => items,
        Ok(v @ Value::Object(_)) => vec![v],
        _ => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn the_touch_is_the_best_of_each_side() {
        let mut b = Book::default();
        b.set(true, 0.40, 100.0);
        b.set(true, 0.42, 50.0);
        b.set(false, 0.47, 10.0);
        b.set(false, 0.45, 20.0);
        assert_eq!(b.best_bid(), Some(0.42));
        assert_eq!(b.best_ask(), Some(0.45));
    }

    #[test]
    fn a_zero_size_removes_the_level() {
        // How the feed says a price is gone. Keeping it would report a touch nobody is offering.
        let mut b = Book::default();
        b.set(true, 0.42, 50.0);
        b.set(true, 0.40, 100.0);
        b.set(true, 0.42, 0.0);
        assert_eq!(b.best_bid(), Some(0.40));
    }

    #[test]
    fn an_empty_side_has_no_touch() {
        let mut b = Book::default();
        b.set(true, 0.42, 50.0);
        assert_eq!(b.best_ask(), None);
        assert!(!b.is_empty());
    }

    #[test]
    fn a_snapshot_replaces_rather_than_merges() {
        // Anything missing from a snapshot is gone; merging would keep stale levels for ever.
        let mut b = Book::default();
        b.set(true, 0.10, 5.0);
        b.apply(&json!({
            "event_type": "book", "asset_id": "a",
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.44", "size": "100"}],
        }));
        assert_eq!(b.best_bid(), Some(0.40));
        assert_eq!(b.best_ask(), Some(0.44));
    }

    #[test]
    fn a_price_change_updates_one_level() {
        let mut b = Book::default();
        b.apply(&json!({
            "event_type": "book", "asset_id": "a",
            "bids": [{"price": "0.40", "size": "100"}], "asks": [{"price": "0.44", "size": "100"}],
        }));
        b.apply(&json!({
            "event_type": "price_change", "asset_id": "a",
            "changes": [{"price": "0.42", "size": "10", "side": "BUY"}],
        }));
        assert_eq!(b.best_bid(), Some(0.42));
        assert_eq!(b.best_ask(), Some(0.44), "the other side is untouched");
    }

    #[test]
    fn a_price_change_can_arrive_inline() {
        let mut b = Book::default();
        b.apply(&json!({
            "event_type": "price_change", "asset_id": "a",
            "price": "0.30", "size": "7", "side": "SELL",
        }));
        assert_eq!(b.best_ask(), Some(0.30));
    }

    #[test]
    fn a_trade_print_does_not_move_the_book() {
        let mut b = Book::default();
        b.set(true, 0.40, 10.0);
        assert!(!b.apply(&json!({"event_type": "last_trade_price", "price": "0.9"})));
        assert_eq!(b.best_bid(), Some(0.40));
    }

    #[test]
    fn prices_keep_three_decimals() {
        let mut b = Book::default();
        b.set(true, 0.005, 1.0);
        b.set(true, 0.004, 1.0);
        assert_eq!(b.best_bid(), Some(0.005));
    }

    #[test]
    fn one_frame_can_carry_several_events() {
        let v = events(r#"[{"event_type":"book","asset_id":"a"},{"event_type":"book","asset_id":"b"}]"#);
        assert_eq!(v.len(), 2);
        assert_eq!(asset_id(&v[1]), Some("b"));
    }

    #[test]
    fn pong_and_noise_carry_no_events() {
        assert!(events("PONG").is_empty());
        assert!(events("pong").is_empty());
        assert!(events("").is_empty());
        assert!(events("not json").is_empty());
    }

    #[test]
    fn a_single_object_is_one_event() {
        let v = events(r#"{"event_type":"price_change","asset_id":"zz"}"#);
        assert_eq!(v.len(), 1);
        assert_eq!(event_type(&v[0]), "price_change");
        assert_eq!(asset_id(&v[0]), Some("zz"));
    }
}
