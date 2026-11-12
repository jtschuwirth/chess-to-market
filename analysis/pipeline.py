"""The pipeline: observation log -> signals -> paper positions -> report.

Pure and deterministic in the records it is given, which is what makes a backtest and a live log
run through identical code (`journey/ARCHITECTURE.md`).

Also the main debugging tool for the Rust side: `health()` says plainly what the log does and does
not contain, so "no signals" can be told apart from "no eval records" or "evals arriving without
wdl_white".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config_defaults import PipelineConfig
from .findability import Candidate, findability
from .observations import Record
from .paper import PaperTrade, implied_legs, simulate
from .triggers import StateTracker, Trigger


@dataclass
class SignalOut:
    game_id: str | None
    ply: int | None
    san: str | None
    ts: float
    trigger: Trigger
    findability: float
    findability_note: str
    room: float | None
    implied: dict[str, float]
    position: PaperTrade | None = None

    @property
    def confidence(self) -> float:
        """Signal strength: market room weighted by how findable the continuation is.

        Room is the gate (`research/game12-forensics.md`: it separates reaction from non-reaction
        11 times in 12); findability weights it (`research/findability-performance.md`: it predicts
        how fast, not whether).
        """
        return (self.room or 0.0) * self.findability


@dataclass
class Health:
    counts: dict[str, int] = field(default_factory=dict)
    span_s: float = 0.0
    evals_with_wdl: int = 0
    evals_without_wdl: int = 0
    evals_with_lines: int = 0
    legs_seen: set[str] = field(default_factory=set)
    depths_seen: set[int] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    suppressed_by_cooldown: int = 0

    def report(self) -> str:
        out = [f"records: {self.counts}", f"span: {self.span_s:.0f}s"]
        if self.depths_seen:
            out.append(f"eval depths: {sorted(self.depths_seen)}")
        out.append(f"evals with wdl_white: {self.evals_with_wdl}, without: {self.evals_without_wdl}")
        out.append(f"evals carrying MultiPV lines: {self.evals_with_lines}")
        out.append(f"market legs: {sorted(self.legs_seen) or '(none)'}")
        if self.suppressed_by_cooldown:
            out.append(f"triggers suppressed by cooldown: {self.suppressed_by_cooldown}")
        # The diagnoses that actually come up when wiring a new producer.
        if not self.counts.get("eval"):
            out.append("!! no eval records — the engine source is not producing (T-04)")
        elif self.evals_without_wdl:
            out.append("!! eval records without wdl_white — set UCI_ShowWDL and normalise to white")
        if not self.counts.get("market"):
            out.append("!! no market records — the Polymarket source is not producing (T-03)")
        if not self.counts.get("ply"):
            out.append("!! no ply records — the Lichess source is not producing (T-02)")
        if self.counts.get("eval") and not self.evals_with_lines:
            out.append("!  evals have no `lines` — findability will abstain; needs MultiPV >= 4")
        if self.counts.get("market") and not self.legs_seen:
            out.append("!  market records have no `leg` — pass --asset-id <leg>=<id> in the Rust CLI")
        return "\n".join(out)


@dataclass
class Result:
    signals: list[SignalOut] = field(default_factory=list)
    health: Health = field(default_factory=Health)
    tape: list[dict[str, Any]] = field(default_factory=list)
    ply_index: dict[tuple[str | None, int | None], str] = field(default_factory=dict)

    @property
    def positions(self) -> list[PaperTrade]:
        return [s.position for s in self.signals if s.position is not None]

    @property
    def pnl_usd(self) -> float:
        return sum(p.pnl_usd for p in self.positions)

    @property
    def fees_usd(self) -> float:
        return sum(p.fees_usd for p in self.positions)


def build_tape(records: list[Record]) -> list[dict[str, Any]]:
    """Market records -> a two-sided tape the paper model can fill against.

    Carries the last known touch forward: a `price_change` on one side does not invalidate the other.
    """
    tape: list[dict[str, Any]] = []
    last: dict[str, dict[str, float]] = {}
    for r in records:
        if r.source != "market":
            continue
        leg = r.payload.get("leg")
        if not leg:
            continue
        d = last.setdefault(leg, {})
        for side, key in (("ask", "best_ask"), ("bid", "best_bid")):
            v = r.payload.get(key)
            if v is not None:
                d[side] = float(v)
        if not d:
            continue
        mid = (d.get("ask", d.get("bid")) + d.get("bid", d.get("ask"))) / 2
        tape.append({"leg": leg, "t": r.ts, "ask": d.get("ask"), "bid": d.get("bid"), "p": mid})
    return tape


def _candidates(rec: Record) -> list[Candidate]:
    out = []
    for ln in rec.lines():
        wdl = ln.get("wdl")
        if not wdl or len(wdl) != 3:
            continue
        out.append(Candidate(
            san=ln.get("san") or ln.get("uci") or "?",
            uci=ln.get("uci") or "",
            win_prob=wdl[0] / 1000.0,
            draw_prob=wdl[1] / 1000.0,
            cp=ln.get("cp"),
            mate_in=ln.get("mate_in"),
        ))
    return out


def run(records: list[Record], cfg: PipelineConfig | None = None) -> Result:
    cfg = cfg or PipelineConfig()
    res = Result()
    h = res.health

    for r in records:
        h.counts[r.source] = h.counts.get(r.source, 0) + 1
        if r.source == "eval":
            d = r.payload.get("depth")
            if isinstance(d, int):
                h.depths_seen.add(d)
            if r.wdl_white():
                h.evals_with_wdl += 1
            else:
                h.evals_without_wdl += 1
            if r.lines():
                h.evals_with_lines += 1
        elif r.source == "market" and r.payload.get("leg"):
            h.legs_seen.add(r.payload["leg"])
        elif r.source == "note":
            h.notes.append(str(r.payload.get("message")))
    if records:
        h.span_s = (records[-1].mono_ms - records[0].mono_ms) / 1000.0

    for r in records:
        if r.source == "ply" and r.payload.get("san"):
            res.ply_index[(r.game_id, r.ply)] = r.payload["san"]

    res.tape = build_tape(records)

    trackers: dict[str, StateTracker] = {}
    for r in records:
        if r.source != "eval":
            continue
        if cfg.eval_depth is not None and r.payload.get("depth") != cfg.eval_depth:
            continue
        wdl = r.wdl_white()
        if wdl is None:
            continue
        key = r.game_id or "?"
        tracker = trackers.setdefault(key, StateTracker(cfg.trigger))
        trig = tracker.update(wdl[0], wdl[1], wdl[2], ts=r.ts)
        if trig is None:
            continue

        f = findability(_candidates(r), cfg.findability)

        implied: dict[str, float] = {}
        room = None
        if cfg.white_leg and cfg.black_leg:
            implied = implied_legs(wdl[0], wdl[1], wdl[2], cfg.white_leg, cfg.black_leg)
            market_now = {}
            for t in res.tape:
                if t["t"] <= r.ts and t.get("ask") is not None:
                    market_now[t["leg"]] = t["ask"]
            gaps = [abs(implied[leg] - market_now[leg]) for leg in implied if leg in market_now]
            room = max(gaps) if gaps else None

        sig = SignalOut(
            game_id=r.game_id, ply=r.ply, san=res.ply_index.get((r.game_id, r.ply)),
            ts=r.ts, trigger=trig, findability=f.score,
            findability_note=f.factors[0].note if f.factors else "",
            room=room, implied=implied,
        )
        if implied and cfg.resolved_leg is not None:
            sig.position = simulate(r.ts, cfg.latency_s, implied, res.tape,
                                    cfg.resolved_leg, cfg.paper)
        res.signals.append(sig)

    h.suppressed_by_cooldown = sum(t.suppressed for t in trackers.values())
    return res
