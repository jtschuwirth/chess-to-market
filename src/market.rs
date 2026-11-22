//! Which outcomes we are watching, and for which game.
//!
//! A market belongs to a **game**, not to a round. A tournament relays thirty boards and each
//! priced game has its own market, so everything downstream is keyed by game id.
//!
//! The CLI accepts one game's market for now — subscribing to several is T-03 work — but nothing
//! in the shape here assumes that, which is the point: the refactor happens once, not later under
//! pressure during a live round.

use std::collections::BTreeMap;

use anyhow::{bail, Result};

/// One game's market: the outcomes being watched, each with the operator's label.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Market {
    pub game_id: String,
    /// `(leg, asset_id)`, in the order given. `white` / `draw` / `black` need no further mapping.
    pub legs: Vec<(String, String)>,
}

impl Market {
    pub fn asset_ids(&self) -> Vec<String> {
        self.legs.iter().map(|(_, id)| id.clone()).collect()
    }

    /// The leg an asset id belongs to.
    pub fn leg_of(&self, asset_id: &str) -> Option<&str> {
        self.legs
            .iter()
            .find(|(_, id)| id == asset_id)
            .map(|(leg, _)| leg.as_str())
    }

    pub fn describe(&self) -> String {
        let legs: Vec<String> = self.legs.iter().map(|(l, _)| l.clone()).collect();
        format!("{} ({})", self.game_id, legs.join(", "))
    }
}

/// Parse one `--market <game_id>:<leg>=<asset_id>` entry.
pub fn parse_entry(s: &str) -> Result<(String, String, String), String> {
    let (game_id, rest) = s
        .split_once(':')
        .ok_or_else(|| format!("expected <game_id>:<leg>=<asset_id>, got `{s}`"))?;
    let (leg, asset_id) = rest
        .split_once('=')
        .ok_or_else(|| format!("expected <game_id>:<leg>=<asset_id>, got `{s}`"))?;
    let (game_id, leg, asset_id) = (game_id.trim(), leg.trim(), asset_id.trim());
    if game_id.is_empty() || leg.is_empty() || asset_id.is_empty() {
        return Err(format!("expected <game_id>:<leg>=<asset_id>, got `{s}`"));
    }
    Ok((game_id.to_string(), leg.to_string(), asset_id.to_string()))
}

/// Group parsed entries into one market per game, in the order the games first appear.
pub fn group(entries: &[(String, String, String)]) -> Vec<Market> {
    let mut order: Vec<String> = Vec::new();
    let mut by_game: BTreeMap<String, Vec<(String, String)>> = BTreeMap::new();
    for (game_id, leg, asset_id) in entries {
        if !order.contains(game_id) {
            order.push(game_id.clone());
        }
        by_game
            .entry(game_id.clone())
            .or_default()
            .push((leg.clone(), asset_id.clone()));
    }
    order
        .into_iter()
        .map(|game_id| Market {
            legs: by_game.remove(&game_id).unwrap_or_default(),
            game_id,
        })
        .collect()
}

/// What this build will actually subscribe to.
///
/// Refuses a second game rather than silently watching one of them: a run that quietly dropped a
/// market would produce a log that looks complete and is not.
pub fn single(entries: &[(String, String, String)]) -> Result<Option<Market>> {
    let markets = group(entries);
    match markets.len() {
        0 => Ok(None),
        1 => Ok(Some(markets.into_iter().next().expect("len 1"))),
        n => bail!(
            "{n} games given to --market ({}), but only one market is supported for now",
            markets
                .iter()
                .map(|m| m.game_id.as_str())
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entries(raw: &[&str]) -> Vec<(String, String, String)> {
        raw.iter().map(|s| parse_entry(s).unwrap()).collect()
    }

    #[test]
    fn parses_a_game_leg_and_id() {
        assert_eq!(
            parse_entry("MUkIEXlH:white=7137").unwrap(),
            ("MUkIEXlH".into(), "white".into(), "7137".into())
        );
    }

    #[test]
    fn trims_whitespace() {
        assert_eq!(
            parse_entry(" g : draw = 99 ").unwrap(),
            ("g".into(), "draw".into(), "99".into())
        );
    }

    #[test]
    fn rejects_an_entry_with_no_game() {
        // The old form. Easy to type from memory, and it would produce a tape with no game.
        assert!(parse_entry("white=7137").is_err());
    }

    #[test]
    fn rejects_an_empty_part() {
        for bad in [":white=7137", "g:=7137", "g:white=", "", "g:white"] {
            assert!(parse_entry(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn legs_of_one_game_group_into_one_market() {
        let m = group(&entries(&["g:white=1", "g:draw=2", "g:black=3"]));
        assert_eq!(m.len(), 1);
        assert_eq!(m[0].game_id, "g");
        assert_eq!(m[0].asset_ids(), ["1", "2", "3"]);
        assert_eq!(m[0].leg_of("2"), Some("draw"));
        assert_eq!(m[0].leg_of("nope"), None);
    }

    #[test]
    fn two_games_are_two_markets() {
        // The shape is plural even while the CLI accepts one, so adding the second is not a
        // refactor during a live round.
        let m = group(&entries(&["a:white=1", "b:white=2", "a:draw=3"]));
        assert_eq!(m.len(), 2);
        assert_eq!(m[0].game_id, "a");
        assert_eq!(m[0].asset_ids(), ["1", "3"]);
        assert_eq!(m[1].asset_ids(), ["2"]);
    }

    #[test]
    fn one_game_is_accepted() {
        let m = single(&entries(&["g:white=1", "g:black=2"])).unwrap().unwrap();
        assert_eq!(m.game_id, "g");
        assert_eq!(m.legs.len(), 2);
    }

    #[test]
    fn no_market_is_not_an_error() {
        assert_eq!(single(&[]).unwrap(), None);
    }

    #[test]
    fn a_second_game_is_refused_rather_than_dropped() {
        let err = single(&entries(&["a:white=1", "b:white=2"])).unwrap_err().to_string();
        assert!(err.contains("only one market"), "{err}");
        assert!(err.contains('a') && err.contains('b'), "names both: {err}");
    }
}
