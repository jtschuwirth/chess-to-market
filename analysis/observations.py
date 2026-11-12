"""Reading the observation log. The Rust side of the contract is in SCHEMA.md.

Design rule: **tolerant of additions, strict about what it uses.** An unknown `source`, or an extra
field, is ignored rather than fatal — the Rust must be free to add fields mid-match without breaking
the analysis. A record that claims a known source but lacks a field the analysis needs is reported as
a problem, not silently dropped, because a silently dropped ply is a hole in a measurement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass(frozen=True)
class Record:
    mono_ms: int
    wall_unix_ms: int
    source: str
    payload: dict[str, Any]
    line_no: int = 0

    # --- convenience accessors, all optional -----------------------------
    @property
    def game_id(self) -> str | None:
        return self.payload.get("game_id")

    @property
    def ply(self) -> int | None:
        return self.payload.get("ply")

    @property
    def ts(self) -> float:
        """Wall-clock seconds. For aligning with market data, never for durations."""
        return self.wall_unix_ms / 1000.0

    def wdl_white(self) -> tuple[float, float, float] | None:
        """`[win, draw, loss]` per-mille from white's perspective, as probabilities."""
        w = self.payload.get("wdl_white")
        if not w or len(w) != 3:
            return None
        return (w[0] / 1000.0, w[1] / 1000.0, w[2] / 1000.0)

    def lines(self) -> list[dict[str, Any]]:
        return self.payload.get("lines") or []


@dataclass
class ParseReport:
    total: int = 0
    kept: int = 0
    unknown_source: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        out = [f"{self.kept}/{self.total} records parsed"]
        if self.unknown_source:
            out.append(f"unknown sources: {self.unknown_source}")
        if self.problems:
            out.append(f"{len(self.problems)} problem(s):")
            out += [f"  {p}" for p in self.problems[:20]]
            if len(self.problems) > 20:
                out.append(f"  ... and {len(self.problems) - 20} more")
        else:
            out.append("no problems")
        return "\n".join(out)


KNOWN = {"ply", "market", "eval", "signal", "note"}

# Fields the analysis actually consumes. Anything else is passed through untouched.
REQUIRED = {
    "ply": ("san",),
    "market": ("leg",),
    "eval": ("wdl_white",),
    "signal": ("kind",),
    "note": ("message",),
}


def parse_line(line: str, line_no: int = 0) -> tuple[Record | None, str | None]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        return None, f"line {line_no}: not JSON ({e.msg})"
    if not isinstance(obj, dict):
        return None, f"line {line_no}: not an object"
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None, f"line {line_no}: missing payload object"
    source = payload.get("source")
    if not isinstance(source, str):
        return None, f"line {line_no}: payload has no source tag"
    for key in ("mono_ms", "wall_unix_ms"):
        if not isinstance(obj.get(key), (int, float)):
            return None, f"line {line_no}: missing or non-numeric {key}"
    rec = Record(int(obj["mono_ms"]), int(obj["wall_unix_ms"]), source, payload, line_no)
    for f_ in REQUIRED.get(source, ()):
        if payload.get(f_) is None:
            return rec, f"line {line_no}: {source} record missing {f_}"
    return rec, None


def read(path: str) -> tuple[list[Record], ParseReport]:
    """Parse a whole log. Returns the usable records and a report of everything odd."""
    rep = ParseReport()
    out: list[Record] = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            rep.total += 1
            rec, problem = parse_line(line, i)
            if problem:
                rep.problems.append(problem)
            if rec is None:
                continue
            if rec.source not in KNOWN:
                rep.unknown_source[rec.source] = rep.unknown_source.get(rec.source, 0) + 1
                continue
            out.append(rec)
            rep.kept += 1
    return out, rep


def tail(path: str, from_line: int = 0) -> Iterator[Record]:
    """Yield records from `from_line` onward. Used by the dashboard to poll a growing log."""
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, start=1):
            if i <= from_line or not line.strip():
                continue
            rec, _ = parse_line(line, i)
            if rec is not None and rec.source in KNOWN:
                yield rec


def check_ordering(records: list[Record]) -> list[str]:
    """Monotonic clock must not go backwards, and plies must not arrive out of order per game."""
    problems: list[str] = []
    prev_mono = -1
    for r in records:
        if r.mono_ms < prev_mono:
            problems.append(f"line {r.line_no}: mono_ms went backwards ({r.mono_ms} < {prev_mono})")
        prev_mono = max(prev_mono, r.mono_ms)
    seen: dict[str, int] = {}
    for r in records:
        if r.source != "ply" or r.ply is None:
            continue
        key = r.game_id or "?"
        last = seen.get(key, 0)
        if r.ply <= last:
            problems.append(f"line {r.line_no}: ply {r.ply} not after {last} in game {key}")
        seen[key] = max(last, r.ply)
    return problems
