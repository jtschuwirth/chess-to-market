"""Combine the heuristics into one score.

Pure: no engine, no network, no clock. Same inputs always give the same score, which is what lets
the same model run over a backtest and over a live log.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import DEFAULT, FindabilityConfig
from .heuristics import REGISTRY
from .model import Candidate, Factor, Findability


def findability(
    candidates: Sequence[Candidate], cfg: FindabilityConfig | None = None
) -> Findability:
    """How likely is a human to find and hold the engine's continuation?

    `candidates` are the MultiPV lines for the position **after** the move under test, best first,
    with `win_prob` from the perspective of the side to move.

    Combination is multiplicative **in odds**: a `base` heuristic supplies the starting odds and
    `modifier` heuristics multiply it, so modifiers are order-independent and there are no clamping
    artefacts. **If no base heuristic contributes, the result is an explicit abstention** — modifiers
    are not allowed to invent a score from nothing.
    """
    cfg = cfg or DEFAULT
    base: Factor | None = None
    modifiers: list[Factor] = []

    for name in cfg.enabled:
        fn = REGISTRY.get(name)
        if fn is None:
            raise KeyError(f"unknown heuristic {name!r}; known: {sorted(REGISTRY)}")
        f = fn(candidates, cfg)
        if f is None:
            continue
        if getattr(fn, "kind", "modifier") == "base":
            if base is None:
                base = f
            else:
                modifiers.append(f)
        else:
            modifiers.append(f)

    if base is None:
        # No base heuristic had anything to say, so there is no score to report. An abstention is
        # information: it means this move is not a findability question at all.
        return Findability(score=0.5, factors=(Factor("abstain", 1.0, "no base heuristic applied"),))

    odds = base.odds_multiplier
    for f in modifiers:
        odds *= f.odds_multiplier
    factors = [base, *modifiers]

    p = odds / (1 + odds)
    p = min(max(p, cfg.floor), cfg.ceiling)
    return Findability(score=p, factors=tuple(factors))
