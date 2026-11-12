"""Inputs to the findability model. Plain data, no engine and no network."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """One MultiPV line for the position *after* the move under test.

    `win_prob` and `draw_prob` come from Stockfish's `wdl` output (per-mille, divided by 1000),
    **from the perspective of the side to move** — i.e. the side that must find the continuation.
    That is the right perspective here: findability is a question about the player who has to move.
    """

    san: str
    uci: str
    win_prob: float
    draw_prob: float
    cp: int | None = None
    mate_in: int | None = None

    @property
    def loss_prob(self) -> float:
        return max(0.0, 1.0 - self.win_prob - self.draw_prob)

    # --- move character, read straight off the SAN -------------------------
    # SAN already encodes everything needed, so no chess library is required here.
    @property
    def is_capture(self) -> bool:
        return "x" in self.san

    @property
    def is_check(self) -> bool:
        return self.san.endswith("+") or self.san.endswith("#")

    @property
    def is_mate(self) -> bool:
        return self.san.endswith("#")

    @property
    def is_promotion(self) -> bool:
        return "=" in self.san

    @property
    def is_quiet(self) -> bool:
        return not (self.is_capture or self.is_check or self.is_promotion)


@dataclass(frozen=True)
class Factor:
    """One heuristic's contribution, kept so a score is explainable rather than a number."""

    name: str
    odds_multiplier: float
    note: str


@dataclass(frozen=True)
class Findability:
    """How likely a human is to find and hold the engine's continuation.

    `score` is a probability in (0, 1). 1.0 means anyone would find it; near 0 means the
    evaluation hangs on something a human plausibly misses, so **the market may be right not to
    move** and the signal should be discounted.
    """

    score: float
    factors: tuple[Factor, ...]

    def explain(self) -> str:
        lines = [f"findability {self.score:.3f}"]
        for f in self.factors:
            lines.append(f"  {f.name:18s} x{f.odds_multiplier:5.2f} odds   {f.note}")
        return "\n".join(lines)
