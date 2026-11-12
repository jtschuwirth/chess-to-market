"""Generate a reference log that satisfies SCHEMA.md.

Two uses:

1. **The Rust has something to match.** Point it at the same game and compare logs record for
   record; a field you got wrong shows up immediately.
2. **The Python and the dashboard are testable before any Rust exists.**

The default scenario is modelled on WCC 2024 game 14: a quiet game, then `55.Rf2??` turns it from
level to decisively lost, and the market repricing arrives ~30 s later. Deterministic — no random
numbers — so two runs produce byte-identical logs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class Step:
    """One ply of a scripted game."""
    san: str
    uci: str
    wdl_white: tuple[int, int, int]
    best: tuple[str, str, tuple[int, int, int]]   # san, uci, wdl from side to move
    second: tuple[str, str, tuple[int, int, int]]
    cp_white: int = 0


# level ... level ... blunder ... forced sequence
SCRIPT: list[Step] = [
    Step("Ba8", "b7a8", (120, 820, 60), ("Kf6", "e5f6", (80, 850, 70)), ("Kd6", "e5d6", (60, 870, 70)), -10),
    Step("Kf6", "e5f6", (110, 830, 60), ("Rf4", "f2f4", (70, 860, 70)), ("Rf3", "f2f3", (60, 870, 70)), -11),
    Step("Rf4", "f2f4", (100, 840, 60), ("Ke5", "f6e5", (70, 860, 70)), ("Kg6", "f6g6", (50, 880, 70)), -12),
    Step("Ke5", "f6e5", (95, 845, 60), ("Ra4", "f4a4", (80, 850, 70)), ("Rf2", "f4f2", (10, 100, 890)), -14),
    # the blunder: white's eval collapses, and only one move punishes it — a capture
    Step("Rf2??", "f4f2", (0, 2, 998), ("Rxf2", "b2f2", (1000, 0, 0)), ("Rb1+", "b2b1", (36, 940, 24)), -591),
    Step("Rxf2", "b2f2", (0, 0, 1000), ("Kxf2", "g1f2", (0, 0, 1000)), ("Kh1", "g1h1", (0, 0, 1000)), -1438),
    Step("Kxf2", "g1f2", (0, 0, 1000), ("Bd5", "e6d5", (1000, 0, 0)), ("Bc4", "e6c4", (1000, 0, 0)), -1440),
]

# market: quiet, then the repricing burst ~30s after the blunder
TAPE = [
    # (seconds from start, leg, bid, ask)
    (0, "gukesh", 0.08, 0.11), (0, "draw", 0.80, 0.84), (0, "ding", 0.06, 0.09),
    (300, "gukesh", 0.08, 0.11), (300, "draw", 0.80, 0.83),
    (620, "gukesh", 0.09, 0.12), (640, "draw", 0.79, 0.83),
    # blunder lands at t=660; nothing moves for 30s
    (680, "gukesh", 0.09, 0.12),
    (692, "gukesh", 0.30, 0.34), (694, "draw", 0.55, 0.60),
    (700, "gukesh", 0.62, 0.66), (702, "draw", 0.28, 0.33),
    (712, "gukesh", 0.88, 0.91), (714, "draw", 0.06, 0.10),
    (760, "gukesh", 0.95, 0.97), (765, "draw", 0.01, 0.04), (770, "ding", 0.00, 0.02),
    (900, "gukesh", 0.97, 0.99),
]

START_UNIX_MS = 1_764_600_000_000
PLY_EVERY_S = 110          # a classical game's rough pace
BLUNDER_INDEX = 4
GAME_ID = "synth-g14"


def _rec(mono_ms: int, payload: dict) -> str:
    return json.dumps({
        "mono_ms": mono_ms,
        "wall_unix_ms": START_UNIX_MS + mono_ms,
        "payload": payload,
    })


def generate(depths: tuple[int, ...] = (12, 16)) -> list[str]:
    """The log, as a list of JSONL lines."""
    lines: list[str] = []

    # Everything goes through one sorted stream. Emitting the startup notes separately made
    # mono_ms run backwards, which `python -m analysis validate` caught on the first run.
    events: list[tuple[int, dict]] = [
        (0, {"source": "note", "message": f"run started (synthetic reference log, game {GAME_ID})"}),
        (1, {"source": "note", "message": "lichess: stream open, round synth-round"}),
        (2, {"source": "note", "message": "polymarket: subscribed to 3 assets"}),
    ]

    for i, st in enumerate(SCRIPT):
        move_ms = (i + 1) * PLY_EVERY_S * 1000
        events.append((move_ms, {
            "source": "ply", "game_id": GAME_ID, "ply": i + 1,
            "san": st.san, "uci": st.uci,
            "fen": "(synthetic)",
            "move_unix_ms": START_UNIX_MS + move_ms,
        }))
        # one eval per depth, arriving at roughly the measured times
        for depth, delay_ms in zip(depths, (70, 1558)):
            events.append((move_ms + delay_ms, {
                "source": "eval", "game_id": GAME_ID, "ply": i + 1,
                "depth": depth, "search_ms": delay_ms,
                "cp_white": st.cp_white, "mate_in": None,
                "wdl_white": list(st.wdl_white),
                "only_move_gap_cp": (st.best[2][0] - st.second[2][0]) * 2,
                "lines": [
                    {"san": st.best[0], "uci": st.best[1], "cp": None, "mate_in": None,
                     "wdl": list(st.best[2])},
                    {"san": st.second[0], "uci": st.second[1], "cp": None, "mate_in": None,
                     "wdl": list(st.second[2])},
                ],
            }))

    for secs, leg, bid, ask in TAPE:
        events.append((secs * 1000, {
            "source": "market", "asset_id": f"synth-{leg}", "leg": leg,
            "event_type": "price_change", "best_bid": bid, "best_ask": ask,
            "raw": {"event_type": "price_change", "synthetic": True},
        }))

    events.append((780_000, {"source": "note", "message": "polymarket: reconnected after 1 attempt"}))

    for mono_ms, payload in sorted(events, key=lambda e: e[0]):
        lines.append(_rec(mono_ms, payload))
    lines.append(_rec(905_000, {"source": "note", "message": "ctrl-c: draining and shutting down"}))
    return lines


def write(path: str) -> int:
    lines = generate()
    with open(path, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln + "\n")
    return len(lines)
