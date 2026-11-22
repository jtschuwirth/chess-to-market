"""Snapshot to JSON shapes. No analysis happens here — only selecting and reshaping.

Every endpoint's body is built in this module, so route handlers stay a URL and a call and the
shapes can be tested without a server.

Two time axes travel with every point, because they answer different questions and mixing them is
the one mistake that silently ruins a latency figure:

- `t`  — seconds since the first record, from the monotonic clock. For durations and plots.
- `ts` — wall-clock unix seconds. Only for lining a point up against the market or a move time.
"""

from __future__ import annotations

from typing import Any

from analysis.config_defaults import PipelineConfig
from analysis.observations import Record
from analysis.pipeline import SignalOut
from analysis.pipeline import tape_for as pipeline_tape_for
from analysis.triggers import StateTracker

from .store import Snapshot


def _eval_point(snap: Snapshot, r: Record) -> dict[str, Any]:
    wdl = r.wdl_white()
    return {
        "t": snap.rel(r),
        "ts": r.ts,
        "ply": r.ply,
        "depth": r.payload.get("depth"),
        "search_ms": r.payload.get("search_ms"),
        "cp_white": r.payload.get("cp_white"),
        "mate_in": r.payload.get("mate_in"),
        "only_move_gap_cp": r.payload.get("only_move_gap_cp"),
        "wdl_white": None if wdl is None else [round(v, 4) for v in wdl],
        "lines": len(r.lines()),
    }


def _ply_point(snap: Snapshot, r: Record) -> dict[str, Any]:
    return {
        "t": snap.rel(r),
        "ts": r.ts,
        "ply": r.ply,
        "san": r.payload.get("san"),
        "uci": r.payload.get("uci"),
        "fen": r.payload.get("fen"),
        "move_unix_ms": r.payload.get("move_unix_ms"),
    }


def _signal_point(snap: Snapshot, s: SignalOut) -> dict[str, Any]:
    pos = s.position
    return {
        "game_id": s.game_id,
        "t": round(s.ts - snap.t0_wall, 3) if snap.records else 0.0,
        "ts": s.ts,
        "ply": s.ply,
        "san": s.san,
        "kind": s.trigger.kind.value,
        "before": s.trigger.before.value,
        "after": s.trigger.after.value,
        "p_white_win": round(s.trigger.p_white_win, 4),
        "p_draw": round(s.trigger.p_draw, 4),
        "p_black_win": round(s.trigger.p_black_win, 4),
        "room": None if s.room is None else round(s.room, 4),
        "findability": round(s.findability, 4),
        "confidence": round(s.confidence, 4),
        "note": s.findability_note,
        "implied": {k: round(v, 4) for k, v in s.implied.items()},
        # Hypothetical throughout: nothing here was ever an order.
        "position": None
        if pos is None
        else {
            "leg": pos.leg,
            "entry_price": round(pos.entry_price, 4),
            "exit_price": round(pos.exit_price, 4),
            "exit_reason": pos.exit_reason,
            "pnl_usd": round(pos.pnl_usd, 4),
            "fees_usd": round(pos.fees_usd, 4),
        },
    }


def game_ids(snap: Snapshot) -> list[str]:
    """Every game the log mentions, in the order it first mentions them."""
    out: list[str] = []
    for r in snap.records:
        gid = r.game_id
        if gid and gid not in out:
            out.append(gid)
    return out


# The PGN result token is the only honest end-of-game signal: most decisive games between titled
# players end in a resignation, so the final position is not mate and the board says nothing.
WINNER = {"1-0": "white", "0-1": "black", "1/2-1/2": "draw"}


def _headers(snap: Snapshot) -> dict[str, dict[str, Any]]:
    """The latest `game` record per id. A finish arrives as a second record for the same game."""
    out: dict[str, dict[str, Any]] = {}
    for r in snap.records:
        if r.source == "game" and r.game_id:
            out[r.game_id] = r.payload
    return out


def _info(game_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Names, result and broadcast link — all optional, so this degrades to the bare id.

    `finished` is `None`, not `False`, when the log carries no `game` record for this id: "nobody
    told us" and "still in play" are different states, and showing the second for the first would
    be a dashboard inventing a fact.
    """
    p = payload or {}
    white, black = p.get("white"), p.get("black")
    result = p.get("result")
    return {
        "title": f"{white} — {black}" if white and black else game_id,
        "white": white,
        "black": black,
        "white_elo": p.get("white_elo"),
        "black_elo": p.get("black_elo"),
        "white_title": p.get("white_title"),
        "black_title": p.get("black_title"),
        "result": result,
        "finished": None if result is None else result in WINNER,
        "winner": WINNER.get(result or ""),
        "board": p.get("board"),
        "event": p.get("event"),
        "url": p.get("url"),
    }


def _evals_of(snap: Snapshot, game_id: str | None, depth: int | None) -> list[Record]:
    return [
        r
        for r in snap.records
        if r.source == "eval"
        and r.wdl_white() is not None
        and (game_id is None or r.game_id == game_id)
        and (depth is None or r.payload.get("depth") == depth)
    ]


def _state_timeline(
    evals: list[Record], cfg: PipelineConfig
) -> tuple[str | None, list[dict[str, Any]]]:
    """Replay the tracker for the current state and every change along the way.

    Uses the same `StateTracker` the pipeline uses, so the dashboard cannot disagree with the
    signals it is displaying. `emitted` says whether that change actually produced a signal —
    a change with `emitted: false` was swallowed by the cooldown, which is worth seeing.
    """
    tracker = StateTracker(cfg.trigger)
    changes: list[dict[str, Any]] = []
    for r in evals:
        wdl = r.wdl_white()
        if wdl is None:
            continue
        before = tracker.state
        trig = tracker.update(wdl[0], wdl[1], wdl[2], ts=r.ts)
        if tracker.state is not before:
            changes.append(
                {
                    "ply": r.ply,
                    "ts": r.ts,
                    "from": None if before is None else before.value,
                    "to": None if tracker.state is None else tracker.state.value,
                    "emitted": trig is not None,
                }
            )
    return (None if tracker.state is None else tracker.state.value), changes


def games(snap: Snapshot, cfg: PipelineConfig) -> list[dict[str, Any]]:
    """One row per game: enough to render a board list and to spot a dead feed."""
    out = []
    headers = _headers(snap)
    priced = {t["game_id"] for t in snap.result.tape if t.get("game_id")}
    for gid in game_ids(snap):
        plies = [r for r in snap.records if r.source == "ply" and r.game_id == gid]
        evals = _evals_of(snap, gid, cfg.eval_depth)
        sigs = [s for s in snap.result.signals if s.game_id == gid]
        last_ply = plies[-1] if plies else None
        last_eval = evals[-1] if evals else None
        state, _ = _state_timeline(evals, cfg)
        wdl = last_eval.wdl_white() if last_eval else None
        out.append(
            _info(gid, headers.get(gid))
            | {
                "game_id": gid,
                "plies": len(plies),
                "evals": len(evals),
                "signals": len(sigs),
                "state": state,
                "last_ply": None if last_ply is None else last_ply.ply,
                "last_san": None if last_ply is None else last_ply.payload.get("san"),
                "last_fen": None if last_ply is None else last_ply.payload.get("fen"),
                "last_move_unix_ms": None
                if last_ply is None
                else last_ply.payload.get("move_unix_ms"),
                "wdl_white": None if wdl is None else [round(v, 4) for v in wdl],
                "last_seen_t": max([snap.rel(r) for r in plies[-1:] + evals[-1:]] or [0.0]),
                "has_market": gid in priced,
                "pnl_usd": round(sum(s.position.pnl_usd for s in sigs if s.position), 4),
            }
        )
    return out


def game(snap: Snapshot, game_id: str, cfg: PipelineConfig) -> dict[str, Any] | None:
    if game_id not in game_ids(snap):
        return None
    plies = [r for r in snap.records if r.source == "ply" and r.game_id == game_id]
    all_evals = _evals_of(snap, game_id, None)
    evals = _evals_of(snap, game_id, cfg.eval_depth)
    state, changes = _state_timeline(evals, cfg)
    sigs = [s for s in snap.result.signals if s.game_id == game_id]
    return _info(game_id, _headers(snap).get(game_id)) | {
        "game_id": game_id,
        "state": state,
        "decision_depth": cfg.eval_depth,
        "depths": sorted(
            {r.payload["depth"] for r in all_evals if isinstance(r.payload.get("depth"), int)}
        ),
        "plies": [_ply_point(snap, r) for r in plies],
        "evals": [_eval_point(snap, r) for r in evals],
        "state_changes": changes,
        "signals": [_signal_point(snap, s) for s in sigs],
        "latency": latency(snap, game_id),
        "market": market(snap, game_id),
    }


def market(snap: Snapshot, game_id: str | None = None) -> dict[str, Any]:
    """The two-sided tape, per leg, for one game.

    A market belongs to a game, not to a round: a tournament can have several priced boards at
    once, and merging them would put one board's price beside another board's evaluation. Omitting
    `game_id` returns every priced game, which is what the overview needs to say which boards have
    a market at all.
    """
    tape = snap.result.tape if game_id is None else pipeline_tape_for(snap.result.tape, game_id)
    series: dict[str, list[dict[str, Any]]] = {}
    for t in tape:
        series.setdefault(t["leg"], []).append(
            {
                "t": round(t["t"] - snap.t0_wall, 3) if snap.records else 0.0,
                "ts": t["t"],
                "bid": t.get("bid"),
                "ask": t.get("ask"),
                "mid": round(t["p"], 4),
            }
        )
    return {
        "game_id": game_id,
        "legs": sorted(series),
        "series": series,
        "last": {leg: pts[-1] for leg, pts in series.items() if pts},
        "games": sorted({t["game_id"] for t in snap.result.tape if t.get("game_id")}),
    }


def signals(snap: Snapshot) -> dict[str, Any]:
    res = snap.result
    return {
        "signals": [_signal_point(snap, s) for s in res.signals],
        "positions": len(res.positions),
        # Computed from recorded prices. Nothing here was ever an order.
        "pnl_usd": round(res.pnl_usd, 4),
        "fees_usd": round(res.fees_usd, 4),
        "hypothetical": True,
    }


def health(snap: Snapshot) -> dict[str, Any]:
    """What the log does and does not contain — the first thing to look at when nothing shows up."""
    h = snap.result.health
    return {
        "log_missing": snap.missing,
        "counts": h.counts,
        "span_s": round(h.span_s, 1),
        "depths_seen": sorted(h.depths_seen),
        "legs_seen": sorted(h.legs_seen),
        "evals_with_wdl": h.evals_with_wdl,
        "evals_without_wdl": h.evals_without_wdl,
        "evals_with_lines": h.evals_with_lines,
        "suppressed_by_cooldown": h.suppressed_by_cooldown,
        # Plain-language diagnoses, including the "!!" lines that name the producer that is silent.
        "report": h.report().split("\n"),
        "schema_problems": snap.report.problems[:50],
        "schema_problem_count": len(snap.report.problems),
        "ordering_problems": snap.ordering[:20],
        "unknown_sources": snap.report.unknown_source,
        "notes": h.notes[-50:],
    }


def recent(snap: Snapshot, limit: int = 100) -> list[dict[str, Any]]:
    """The raw tail, one line each — for watching a feed come alive."""
    return [
        {
            "t": snap.rel(r),
            "ts": r.ts,
            "source": r.source,
            "game_id": r.game_id,
            "text": describe(r),
        }
        for r in snap.records[-limit:]
    ]


def describe(r: Record) -> str:
    p = r.payload
    if r.source == "game":
        return f"{p.get('white') or '?'} — {p.get('black') or '?'}  {p.get('result') or ''}".strip()
    if r.source == "ply":
        return f"ply {p.get('ply')} {p.get('san')}"
    if r.source == "eval":
        w = r.wdl_white()
        wdl = f"W{w[0]:.2f} D{w[1]:.2f} L{w[2]:.2f}" if w else "no wdl_white"
        return f"d{p.get('depth')} ply {p.get('ply')} {wdl} ({len(r.lines())} lines)"
    if r.source == "market":
        return (
            f"{p.get('leg')} {p.get('event_type')} "
            f"bid={p.get('best_bid')} ask={p.get('best_ask')}"
        )
    if r.source == "signal":
        return f"{p.get('kind')} conf={p.get('confidence')} {p.get('reason')}"
    return str(p.get("message", ""))


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank. With a handful of moves an interpolated percentile invents precision."""
    if not sorted_values:
        return 0.0
    i = max(0, min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[i]


def latency(snap: Snapshot, game_id: str) -> dict[str, Any]:
    """How long this program took to react to each move — the one number we control.

    **Measured on `mono_ms`, and only within a run.** This is a duration, which is what the
    monotonic clock is for, and the two records either side of a restart are hours apart on the
    wall clock while being nothing of the sort.

    Per ply:

    - `total_ms` — logging the move to its evaluation being available. The figure a code change
      has to move: the lag between the information existing and us able to act on it.
    - `searches` — what the engine says each depth's search cost.
    - `queue_ms` — `total_ms` minus the deepest search. Time this position spent *not* being
      searched: waiting behind other boards, parsing, channel hops.

    It excludes Lichess's own relay delay, which is theirs and invisible from in here.

    `depths_emitted_together` is worth watching. The engine collects its depths and sends them when
    the search ends, so depth 12's verdict does not arrive when depth 12 finishes — it waits for
    depth 16. That makes the cheap early depth useless for acting early, which was the reason for
    emitting it.
    """
    runs = [(r["first_index"], r["last_index"]) for r in snap.runs] or [(0, len(snap.records) - 1)]

    def run_of(i: int) -> int:
        for n, (a, b) in enumerate(runs):
            if a <= i <= b:
                return n
        return 0

    moves: dict[tuple[int, int], Record] = {}
    evals: dict[tuple[int, int], list[Record]] = {}
    for i, r in enumerate(snap.records):
        if r.game_id != game_id or r.ply is None:
            continue
        key = (run_of(i), r.ply)
        if r.source == "ply":
            moves.setdefault(key, r)
        elif r.source == "eval" and key in moves:
            evals.setdefault(key, []).append(r)

    rows: list[dict[str, Any]] = []
    emitted_together = 0
    for key in sorted(evals, key=lambda k: (k[0], k[1])):
        move, group = moves[key], evals[key]
        searches = {
            r.payload["depth"]: r.payload.get("search_ms")
            for r in group
            if isinstance(r.payload.get("depth"), int)
        }
        first = min(r.mono_ms for r in group) - move.mono_ms
        total = max(r.mono_ms for r in group) - move.mono_ms
        deepest = max((v for v in searches.values() if v), default=0)
        if len({r.mono_ms for r in group}) == 1 and len(group) > 1:
            emitted_together += 1
        rows.append(
            {
                "ply": key[1],
                "first_eval_ms": max(0, first),
                "total_ms": max(0, total),
                "queue_ms": max(0, total - deepest),
                "searches": {str(d): v for d, v in sorted(searches.items())},
            }
        )

    totals = sorted(r["total_ms"] for r in rows)
    queues = sorted(r["queue_ms"] for r in rows)
    depths: dict[str, list[int]] = {}
    for r in rows:
        for d, v in r["searches"].items():
            if v:
                depths.setdefault(d, []).append(v)

    return {
        "per_ply": rows,
        "summary": {
            "n": len(rows),
            "median_ms": _percentile(totals, 0.5),
            "p90_ms": _percentile(totals, 0.9),
            "max_ms": totals[-1] if totals else 0,
            "median_queue_ms": _percentile(queues, 0.5),
            "searches": [
                {"depth": int(d), "median_ms": _percentile(sorted(v), 0.5), "n": len(v)}
                for d, v in sorted(depths.items(), key=lambda kv: int(kv[0]))
            ],
        },
        # True when every depth of a position reaches the log at the same instant, which means the
        # cheap early depth buys no earliness at all.
        "depths_emitted_together": emitted_together,
        "measured": "mono_ms, within a run",
    }
