"""Finding the logs on disk, and labelling them.

The Rust writes `logs/<tournament-slug>/<round-slug>.jsonl`, so the directory already holds the
structure the dashboard wants: one event, several rounds. This reads that structure back.

A slug is not a title — `fide-world-junior-chess-championship-2026-open` is unreadable in a dropdown
— so each log is labelled from its own contents. The first `game` record carries `event` verbatim
from the relay, which is the name a human would recognise. Only the head of a file is read: the
label is in the first few records or it is not worth hunting for.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# How far into a file to look for a label. Game records arrive before any move, so this is generous.
LABEL_SCAN_LINES = 200

# Logs that are not a round of a tournament: fixtures, reference logs, one-offs at the root.
LOOSE = "_loose"


@dataclass(frozen=True)
class LogFile:
    """One log file, as the picker needs it."""

    path: str
    event_slug: str
    event_name: str
    round_slug: str
    round_name: str
    size: int
    mtime: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def prettify(slug: str) -> str:
    """`round-5` to `Round 5`. Good enough for a label, never used as an identity."""
    return " ".join(w.capitalize() if w.isalpha() else w for w in slug.split("-")) or slug


def read_labels(path: Path) -> tuple[str | None, str | None]:
    """`(event_name, round_name)` from the head of a log, or `(None, None)`.

    Tolerant on purpose: a half-written line, a truncated file or a log from an older build should
    cost a label, never an error page.
    """
    event = round_name = None
    try:
        with path.open(encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= LABEL_SCAN_LINES or (event and round_name):
                    break
                try:
                    payload = json.loads(line).get("payload") or {}
                except (json.JSONDecodeError, AttributeError):
                    continue
                if payload.get("source") == "game" and payload.get("event"):
                    event = event or str(payload["event"])
                elif payload.get("source") == "note":
                    # "lichess: <tournament> · <round> (<id>), startsAt <ms>"
                    msg = str(payload.get("message", ""))
                    if msg.startswith("lichess: ") and " · " in msg and "startsAt" in msg:
                        head, _, tail = msg[len("lichess: ") :].partition(" · ")
                        event = event or head.strip()
                        round_name = round_name or tail.split("(")[0].strip()
    except OSError:
        return None, None
    return event, round_name


def describe(root: Path, path: Path) -> LogFile:
    rel = path.relative_to(root)
    parts = rel.as_posix().split("/")
    event_slug = parts[0] if len(parts) > 1 else LOOSE
    round_slug = path.stem
    event_name, round_name = read_labels(path)
    stat = path.stat()
    return LogFile(
        path=rel.as_posix(),
        event_slug=event_slug,
        event_name=event_name or (prettify(event_slug) if event_slug != LOOSE else "Other logs"),
        round_slug=round_slug,
        round_name=round_name or prettify(round_slug),
        size=stat.st_size,
        mtime=stat.st_mtime,
    )


def discover(root: Path, extra: Path | None = None) -> list[LogFile]:
    """Every `.jsonl` under `root`, newest first.

    One level deep plus the root itself, which is exactly the shape the Rust writes. Going deeper
    would only invite a stray directory of something else into the picker.
    """
    found: dict[str, Path] = {}
    if root.is_dir():
        for p in sorted(root.glob("*.jsonl")):
            found[p.resolve().as_posix()] = p
        for d in sorted(x for x in root.iterdir() if x.is_dir()):
            for p in sorted(d.glob("*.jsonl")):
                found[p.resolve().as_posix()] = p

    # A log named with --log can live anywhere; it still belongs in the picker.
    if extra is not None and extra.is_file():
        key = extra.resolve().as_posix()
        if key not in found:
            try:
                extra.relative_to(root)
                found[key] = extra
            except ValueError:
                found[key] = extra

    out = []
    for p in found.values():
        try:
            out.append(describe(root, p) if _under(root, p) else _describe_outsider(p))
        except OSError:
            continue
    out.sort(key=lambda f: f.mtime, reverse=True)
    return out


def _under(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _describe_outsider(path: Path) -> LogFile:
    stat = path.stat()
    event, round_name = read_labels(path)
    return LogFile(
        path=path.as_posix(),
        event_slug=LOOSE,
        event_name=event or "Other logs",
        round_slug=path.stem,
        round_name=round_name or prettify(path.stem),
        size=stat.st_size,
        mtime=stat.st_mtime,
    )


def resolve(root: Path, rel: str, allowed: set[str]) -> Path | None:
    """Turn a client-supplied path into a real one, or `None`.

    `rel` arrives from a query string, so it decides which file the server opens — the one place in
    this API where a request chooses a path. Only paths already listed by `discover` are accepted,
    which rules out traversal without having to reason about `..` at all.
    """
    if rel not in allowed:
        return None
    candidate = Path(rel)
    path = candidate if candidate.is_absolute() else root / candidate
    return path if path.is_file() else None


def grouped(files: list[LogFile]) -> list[dict[str, Any]]:
    """Events, newest first, each with its rounds."""
    events: dict[str, dict[str, Any]] = {}
    for f in files:
        ev = events.setdefault(
            f.event_slug,
            {"slug": f.event_slug, "name": f.event_name, "mtime": f.mtime, "rounds": []},
        )
        ev["rounds"].append(f.to_json())
        ev["mtime"] = max(ev["mtime"], f.mtime)
        if f.event_slug != LOOSE:
            ev["name"] = f.event_name
    return sorted(events.values(), key=lambda e: e["mtime"], reverse=True)
