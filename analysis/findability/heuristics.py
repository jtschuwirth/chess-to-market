"""The heuristics themselves.

Adding one is two steps: write a function with this signature, then put its name in `REGISTRY` and
in `FindabilityConfig.enabled`. Nothing else in the pipeline changes.

    @heuristic("base")        # or "modifier"
    def my_heuristic(candidates, cfg) -> Factor | None

Return `None` to abstain.

**A base heuristic answers "how findable is this, roughly?" and supplies the starting odds. A
modifier only adjusts.** The distinction matters: if every base abstains there is nothing to modify,
and a modifier must not be allowed to invent a score out of nothing — a dead-drawn position would
otherwise come back as "0.5, because the move is quiet", which reads like a judgement when it is
actually an absence of one.

The first base to contribute wins, so **order in `cfg.enabled` is the priority order for bases**.
Modifiers compose multiplicatively in odds and are order-independent.

## Why the two bases are mutually exclusive

`forcing_move` runs first and `quiet_holding_moves` only sees quiet positions. A professional
considers every capture and check as a matter of course, so when the refutation is forcing it does
not matter how many alternatives also hold — it gets found. Counting holding moves is only
informative when the move that holds is **quiet**, because that is the case where a human can
genuinely look past it.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import FindabilityConfig
from .model import Candidate, Factor


def _odds(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return p / (1 - p)


def heuristic(kind: str):
    """Mark a heuristic as supplying the base odds, or as only adjusting them."""
    if kind not in ("base", "modifier"):
        raise ValueError(f"kind must be 'base' or 'modifier', got {kind!r}")

    def deco(fn):
        fn.kind = kind
        return fn

    return deco


def _winning(candidates: Sequence[Candidate], cfg: FindabilityConfig) -> bool:
    """Is there an advantage to hold at all?

    Findability is a question about keeping a win. If nobody is winning there is nothing to find,
    and both bases abstain rather than returning a number that means nothing.
    """
    return bool(candidates) and candidates[0].win_prob >= cfg.hold_min_win_prob


@heuristic("base")
def forcing_move(candidates: Sequence[Candidate], cfg: FindabilityConfig) -> Factor | None:
    """H2 — the best move is forcing, so assume it gets found.

    Captures and checks are on every professional's candidate list before anything else. When the
    move that holds the win is forcing, the number of alternatives is irrelevant: this is why game
    14's `Rxf2` was repriced within 31 seconds despite being the *only* move that won.

    Runs before `quiet_holding_moves` and makes it moot when it fires.
    """
    if not _winning(candidates, cfg):
        return None
    best = candidates[0]
    if best.is_mate:
        return Factor("forcing_move", _odds(cfg.mate_base), f"{best.san} is mate")
    if best.is_capture and best.is_check:
        return Factor("forcing_move", _odds(cfg.capture_check_base), f"{best.san} is capture+check")
    if best.is_check:
        return Factor("forcing_move", _odds(cfg.check_base), f"{best.san} is check")
    if best.is_capture:
        return Factor("forcing_move", _odds(cfg.capture_base), f"{best.san} is capture")
    if best.is_promotion:
        return Factor("forcing_move", _odds(cfg.promotion_base), f"{best.san} is promotion")
    return None  # quiet — hand over to quiet_holding_moves


@heuristic("base")
def quiet_holding_moves(candidates: Sequence[Candidate], cfg: FindabilityConfig) -> Factor | None:
    """H1 — for a QUIET best move, how many moves hold the advantage?

    Only meaningful when the move that holds is quiet. If several quiet moves keep the win, a human
    is unlikely to go wrong. If exactly one does, the evaluation hangs on a resource that can be
    looked straight past — and the market may be correct to ignore it.

    **Needs MultiPV ≥ 4.** At MultiPV 2 the count saturates at 2 and "two moves hold" cannot be
    distinguished from "every move holds".
    """
    if not _winning(candidates, cfg):
        return None
    best = candidates[0]
    if not best.is_quiet:
        return None  # forcing_move owns this case

    holding = [
        c
        for c in candidates
        if c.win_prob >= cfg.hold_min_win_prob
        and (best.win_prob - c.win_prob) <= cfg.hold_tolerance
    ]
    n = len(holding)
    base = cfg.hold_base[min(n, len(cfg.hold_base)) - 1]
    quiet_holders = sum(1 for c in holding if c.is_quiet)
    note = (
        f"quiet best move; {n} of {len(candidates)} lines hold "
        f"({quiet_holders} of them quiet)"
    )
    if n >= len(candidates):
        note += " — count capped by MultiPV, true count may be higher"
    return Factor("quiet_holding_moves", _odds(base), note)


# --- ideas for later, deliberately not implemented -------------------------
# tactical_depth   — plies until the point of the line lands. A forcing move is *considered*, but a
#                    long forcing line still has to be calculated correctly to the end
# piece_count      — fewer pieces, easier to calculate exhaustively
# time_pressure    — clock remaining for the side to move (we already log %clk)
# backward_move    — retreating or backward moves are notoriously easy to overlook
# quiet_king_move  — a quiet king move as the only resource is the classic miss

REGISTRY = {
    "forcing_move": forcing_move,
    "quiet_holding_moves": quiet_holding_moves,
}
