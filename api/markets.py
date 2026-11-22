"""Choosing which markets the running tool watches.

This is the one part of the API that writes anything, and it is worth being exact about what that
means. It writes **one local file** naming public market-data feeds to subscribe to. It still holds
no key, signs nothing, and places no order — the tool it is steering has no ability to.

The file is the contract with the Rust side (`src/control.rs`): desired state, not commands, read
beside the round's log as `<log stem>.markets.json`.

A pasted link is resolved through Polymarket's public Gamma API. **The link is never fetched** — a
slug is extracted from it and used to query a fixed host, so a URL from the browser cannot make
this server fetch an arbitrary address.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

GAMMA = "https://gamma-api.polymarket.com"
TIMEOUT_S = 10

# Slugs are lowercase, digits and hyphens. Anything else is not a slug and is not sent anywhere.
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,120}$")


class ResolveError(Exception):
    """The link could not be turned into a market. The message is shown to the operator."""


def control_path(log: Path) -> Path:
    """Mirror of `control::path_for` in the Rust. The two must agree or nothing happens."""
    return log.with_name(f"{log.stem}.markets.json")


def slugs_from(source: str) -> list[str]:
    """Candidate slugs in a pasted link, most specific first.

    Accepts a bare slug, `polymarket.com/event/<event>`, and
    `polymarket.com/event/<event>/<market>`. The last path segment is usually the market and the
    one before it the event, so both are tried.
    """
    text = (source or "").strip()
    if not text:
        raise ResolveError("paste a Polymarket link or a market slug")

    if "://" in text or text.startswith("www.") or "polymarket.com" in text:
        parsed = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
        host = (parsed.hostname or "").lower()
        if host and not (host == "polymarket.com" or host.endswith(".polymarket.com")):
            raise ResolveError(f"{host} is not a Polymarket link")
        parts = [p for p in parsed.path.split("/") if p and p not in ("event", "market", "markets")]
    else:
        parts = [p for p in text.split("/") if p]

    out = [p.lower() for p in reversed(parts) if SLUG.match(p.lower())]
    if not out:
        raise ResolveError(f"no market slug found in {source!r}")
    return out


def _get(path: str, **params: str) -> Any:
    url = f"{GAMMA}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 - fixed host above
        return json.loads(r.read().decode("utf-8"))


def _outcomes(market: dict[str, Any]) -> list[dict[str, Any]]:
    """`outcomes` and `clobTokenIds` are JSON strings *inside* the JSON. Parse them twice."""

    def inner(key: str) -> list[str]:
        raw = market.get(key)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return []
        return [str(x) for x in raw] if isinstance(raw, list) else []

    names, ids = inner("outcomes"), inner("clobTokenIds")
    return [
        {"outcome": n, "asset_id": i}
        for n, i in zip(names, ids)
        if i  # a market with no token id cannot be subscribed to
    ]


def _market_json(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": m.get("slug"),
        "question": m.get("question"),
        "active": bool(m.get("active")),
        "closed": bool(m.get("closed")),
        "best_bid": m.get("bestBid"),
        "best_ask": m.get("bestAsk"),
        "outcomes": _outcomes(m),
    }


def resolve(source: str) -> dict[str, Any]:
    """A pasted link to the markets it names, with their outcomes and asset ids."""
    tried: list[str] = []
    for slug in slugs_from(source):
        tried.append(slug)
        markets = _get("/markets", slug=slug)
        if isinstance(markets, list) and markets:
            return {"source": source, "slug": slug, "markets": [_market_json(m) for m in markets]}

        events = _get("/events", slug=slug)
        if isinstance(events, list) and events:
            found = [_market_json(m) for e in events for m in (e.get("markets") or [])]
            if found:
                return {
                    "source": source,
                    "slug": slug,
                    "event": events[0].get("title"),
                    "markets": found,
                }
    raise ResolveError(f"Polymarket has no market for {' or '.join(tried)}")


class LegIn(BaseModel):
    """One outcome of a market, labelled by the operator."""

    leg: str = Field(description="white, draw or black — or any label you map yourself")
    asset_id: str


class MarketIn(BaseModel):
    legs: list[LegIn] = Field(default_factory=list)
    enabled: bool = True
    source: str | None = Field(default=None, description="what was pasted, kept for the UI")


# --- the control file -------------------------------------------------


@dataclass(frozen=True)
class Entry:
    game_id: str
    enabled: bool
    legs: list[dict[str, str]]
    source: str | None = None

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "game_id": self.game_id,
            "enabled": self.enabled,
            "legs": self.legs,
        }
        if self.source:
            body["source"] = self.source
        return body


def read_control(log: Path) -> list[dict[str, Any]]:
    path = control_path(log)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    markets = body.get("markets") if isinstance(body, dict) else None
    return markets if isinstance(markets, list) else []


def write_control(log: Path, markets: list[dict[str, Any]]) -> None:
    """Write the file the Rust reads.

    Written to a temporary file and renamed, because the reader is another process polling it. A
    rename is atomic, so the Rust never sees half a file — it tolerates a torn read, but there is
    no reason to hand it one.
    """
    path = control_path(log)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"markets": markets}, indent=2), encoding="utf-8")
    tmp.replace(path)


def upsert(log: Path, entry: Entry) -> list[dict[str, Any]]:
    """Add or replace one game's market, keeping the order stable."""
    markets = read_control(log)
    for i, m in enumerate(markets):
        if m.get("game_id") == entry.game_id:
            markets[i] = entry.to_json()
            break
    else:
        markets.append(entry.to_json())
    write_control(log, markets)
    return markets


def remove(log: Path, game_id: str) -> list[dict[str, Any]]:
    markets = [m for m in read_control(log) if m.get("game_id") != game_id]
    write_control(log, markets)
    return markets
