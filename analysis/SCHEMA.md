# The observation log — contract between Rust and Python

The Rust tool appends JSONL. Python reads it. **This file is the interface**, and it is the only thing
either side needs to agree on.

One JSON object per line. Every record:

```json
{"mono_ms": 12345, "wall_unix_ms": 1764000001200, "payload": { "source": "...", ... }}
```

- `mono_ms` — milliseconds since process start, monotonic. **The only field valid for durations.**
- `wall_unix_ms` — system clock, for lining up against the real world. Never subtract it.
- `payload.source` — the tag that selects the variant below.

Unknown `source` values and unknown extra fields are **ignored, not fatal**. That lets the Rust add
fields without breaking the Python mid-match.

---

## `ply` — a newly seen move (T-02)

```json
{"source": "ply", "game_id": "wcc26-g1", "ply": 109, "san": "Rf2??", "uci": "f1f2",
 "fen": "B7/8/4b3/4kp2/6p1/6P1/1r3R2/6K1 b - - 17 55",
 "move_unix_ms": 1764000001200}
```

`move_unix_ms` is the **reconstructed** wall-clock time of the move (`startsAt + Σ clock deltas`), not
when the relay delivered it. That distinction is the whole measurement: arrival time would measure
Lichess's lag, not the market's.

`ply` counts half-moves from 1. One record per *newly seen* ply — re-sent plies emit nothing.

## `market` — a Polymarket event (T-03)

```json
{"source": "market", "asset_id": "7137...", "leg": "gukesh", "event_type": "price_change",
 "best_bid": 0.41, "best_ask": 0.44, "raw": {"...": "..."}}
```

`leg` is the **operator's label** for the outcome, and it must be supplied — Python cannot infer which
outcome an `asset_id` is. So the Rust CLI takes labelled ids:

```
--asset-id gukesh=7137... --asset-id draw=9922... --asset-id sindarov=1044...
```

Conventional leg names: a player name, or `draw`. Keep them stable within a run.

`event_type` passes Polymarket's own value through (`book`, `price_change`, `last_trade_price`,
`tick_size_change`). `best_bid`/`best_ask` are the touch where known, else `null`.

## `eval` — an engine verdict (T-04)

```json
{"source": "eval", "game_id": "wcc26-g1", "ply": 109, "depth": 16, "search_ms": 1558,
 "cp_white": -591, "mate_in": null,
 "wdl_white": [0, 2, 998],
 "only_move_gap_cp": 547,
 "lines": [{"san": "Rxf2", "uci": "b2f2", "cp": 667, "mate_in": null, "wdl": [1000, 0, 0]},
           {"san": "Rb1+", "uci": "b2b1", "cp": 40,  "mate_in": null, "wdl": [36, 940, 24]}]}
```

- **`wdl_white` is required** and is `[win, draw, loss]` in per-mille **from white's perspective**.
  Stockfish reports from the side to move, so the Rust normalises. Everything downstream reasons in
  probability space; `cp_white` is kept for the log, not for decisions.
- `lines` carries the MultiPV candidates **for the position after this ply**, best first, each `wdl`
  **from the side to move** — that is the perspective the findability model needs ("can the player who
  must move find it?"). **MultiPV ≥ 4**, or the holding-move count saturates.
- Emit **one record per completed depth of interest** (12 and 16), not one at the end. A full
  four-line set lands at depth 12 in ~70 ms and depth 16 in ~1.6 s.

## `signal` — the decision stage (T-06)

```json
{"source": "signal", "game_id": "wcc26-g1", "ply": 109, "kind": "decisive",
 "confidence": 0.82, "reason": "level->black_winning; room 0.96; findable 0.90",
 "state_before": "level", "state_after": "black_winning"}
```

Python can also derive signals itself from `eval` records — the same `StateTracker` either way. A
`signal` record from the Rust lets the two be compared, which is how the port gets verified.

## `note` — anything the program says about itself

```json
{"source": "note", "message": "polymarket: reconnected after 2 attempts (backoff 4s)"}
```

Source start, death, reconnect, dropped stale positions, cooldown suppressions. **Gaps in the data
must be explainable afterwards**, or the lag figures around them cannot be trusted.

---

## Rust status: ✅ applied (12 Nov 2026)

`src/observation.rs` now matches this contract — `move_unix_ms`, `leg`, `wdl_white`, `search_ms`,
`lines`, and a `Signal` variant. The CLI takes `--asset-id <leg>=<id>` and `--multipv` (default 4).
`cargo test` covers all of it (10 tests), and the fixture is a worked example of this format.

Cross-checked both directions:

```
python -m analysis validate fixtures/sample_run.jsonl   ->  OK — log satisfies the contract
python -m analysis replay   fixtures/sample_run.jsonl   ->  derives the same signal the
                                                            fixture's own `signal` record declares
```

That second line is the verification that matters: the Python derived `decisive: level →
black_winning` on ply 3 from the `eval` records alone, independently of the `signal` record the Rust
wrote. When the real decision stage lands (T-06), the same comparison proves the port rather than
assuming it.

## Verifying the two sides agree

```bash
cd code && python -m analysis validate logs/run.jsonl     # schema + ordering checks
cd code && python -m analysis replay   logs/run.jsonl     # triggers, positions, summary
cd code && python -m analysis serve    logs/run.jsonl     # live dashboard on :8787
```

`python -m analysis synth --out logs/demo.jsonl` writes a **reference log** that satisfies this
contract. Point the Rust at the same games and the two logs should be comparable record for record —
that is the cheapest way to find a field you got wrong.
