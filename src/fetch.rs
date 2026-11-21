//! HTTP with the two checks that matter, so a wrong URL fails loudly instead of returning noise.
//!
//! Lichess answers a bad path with a **200-shaped experience and a 404 status**: an 11 KB HTML
//! page, `Content-Type: text/html`. Hand that to a PGN parser and you get a confusing parse error
//! fifty lines away from the actual mistake — a missing `.pgn` suffix, or a round id that does not
//! exist. Checking the status and the content type turns that into one clear message at the call
//! site.
//!
//! Returns the [`Response`] rather than a `String` on purpose: T-02 needs `.bytes_stream()`, since
//! a live broadcast round never finishes and `.text()` would wait forever.

use anyhow::{bail, Context, Result};
use reqwest::header::CONTENT_TYPE;
use reqwest::Response;

/// What Lichess serves for a PGN export.
pub const PGN: &str = "application/x-chess-pgn";
/// What the JSON endpoints serve.
pub const JSON: &str = "application/json";
/// What a streaming NDJSON endpoint serves.
pub const NDJSON: &str = "application/x-ndjson";

/// The PGN export URL for a broadcast round.
///
/// Exists so the `.pgn` suffix cannot be forgotten: without it the path is not a route at all and
/// Lichess serves its HTML 404 page.
pub fn round_pgn_url(round_id: &str) -> String {
    format!("https://lichess.org/api/broadcast/round/{round_id}.pgn")
}

/// Metadata for one broadcast tournament, including every round and its id.
pub fn tour_url(tour_id: &str) -> String {
    format!("https://lichess.org/api/broadcast/{tour_id}")
}

/// Active, upcoming and past broadcasts — where round ids come from.
pub fn top_broadcasts_url() -> &'static str {
    "https://lichess.org/api/broadcast/top"
}

/// Status and content-type check, split out so it is testable without a network.
///
/// Deliberately `starts_with`: real headers carry a charset, e.g.
/// `application/x-chess-pgn; charset=utf-8`.
pub fn validate(url: &str, status: u16, content_type: &str, expect: &str) -> Result<()> {
    if status == 404 {
        bail!(
            "GET {url} -> 404. Lichess serves an HTML page for an unknown route, so check two \
             things: that a PGN URL ends in `.pgn`, and that the round id exists. Round ids come \
             from https://lichess.org/api/broadcast/top, or the last path segment of a broadcast \
             URL on the site."
        );
    }
    if !(200..300).contains(&status) {
        bail!("GET {url} -> HTTP {status} (content-type `{content_type}`)");
    }
    if !content_type.starts_with(expect) {
        bail!(
            "GET {url} -> HTTP {status}, but content-type is `{content_type}` and `{expect}` was \
             expected. An HTML body here means the route was wrong and Lichess answered with a page."
        );
    }
    Ok(())
}

/// GET a URL, failing with a useful message unless it returned success *and* the expected type.
pub async fn get_checked(url: &str, expect: &str) -> Result<Response> {
    let res = reqwest::get(url)
        .await
        .with_context(|| format!("GET {url} failed before a response arrived"))?;
    let status = res.status().as_u16();
    let content_type = res
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();
    validate(url, status, &content_type, expect)?;
    Ok(res)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_a_pgn_url_with_the_suffix() {
        assert_eq!(
            round_pgn_url("xLgOJ6cb"),
            "https://lichess.org/api/broadcast/round/xLgOJ6cb.pgn"
        );
    }

    #[test]
    fn accepts_pgn_with_a_charset() {
        // The header really is `application/x-chess-pgn; charset=utf-8`.
        assert!(validate("u", 200, "application/x-chess-pgn; charset=utf-8", PGN).is_ok());
    }

    #[test]
    fn rejects_the_html_404_page_with_an_actionable_message() {
        let err = validate("u", 404, "text/html; charset=utf-8", PGN).unwrap_err();
        let msg = format!("{err}");
        assert!(msg.contains(".pgn"), "should name the missing suffix: {msg}");
        assert!(msg.contains("broadcast/top"), "should say where ids come from: {msg}");
    }

    #[test]
    fn rejects_html_served_with_a_200() {
        // Belt and braces: a 200 carrying HTML is still the wrong route.
        let err = validate("u", 200, "text/html", PGN).unwrap_err();
        assert!(format!("{err}").contains("content-type"));
    }

    #[test]
    fn rejects_other_failures_without_the_404_advice() {
        let msg = format!("{}", validate("u", 429, "text/html", PGN).unwrap_err());
        assert!(msg.contains("429"));
        assert!(!msg.contains(".pgn"), "rate limiting is not a URL problem: {msg}");
    }

    #[test]
    fn missing_content_type_header_is_a_failure_not_a_pass() {
        assert!(validate("u", 200, "", PGN).is_err());
    }
}
