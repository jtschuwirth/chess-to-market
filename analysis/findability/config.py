"""Tunable constants for the findability model.

**Every number here is a first guess, not a fitted value.** T-P03 fits them against the historical
dataset and then freezes the result. They live in one place precisely so tweaking is a one-line
change rather than a hunt through the code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FindabilityConfig:
    # --- shared guard -----------------------------------------------------
    # Findability is about holding a win. Below this win probability there is no advantage to hold,
    # and every base heuristic abstains rather than returning a meaningless number.
    hold_min_win_prob: float = 0.50

    # --- H2 `forcing_move`: base findability when the best move is forcing ---
    # A professional considers every capture and check as a matter of course, so a forcing
    # refutation gets found regardless of how many alternatives exist.
    mate_base: float = 0.98
    capture_check_base: float = 0.95
    check_base: float = 0.92
    capture_base: float = 0.90
    promotion_base: float = 0.85

    # --- H1 `quiet_holding_moves`: base findability when the best move is QUIET ---
    # A line "holds" if its win probability is within `hold_tolerance` of the best line's.
    hold_tolerance: float = 0.15
    # Indexed by holding count: 0 => 1 move, 1 => 2, 2 => 3, 3+ => 4 or more.
    # One quiet move means a resource that can be looked straight past; several means it is hard
    # to go wrong even without seeing the best one.
    hold_base: tuple[float, ...] = (0.30, 0.60, 0.85, 0.95)

    # --- guards -----------------------------------------------------------
    # Clamp the final probability away from 0 and 1: no heuristic is certain, and a score of
    # exactly 1.0 would let the rest of the pipeline stop thinking.
    floor: float = 0.02
    ceiling: float = 0.99

    # Order matters for bases: the first one to contribute supplies the odds. `forcing_move` must
    # come first, so that `quiet_holding_moves` only ever sees quiet positions.
    enabled: tuple[str, ...] = ("forcing_move", "quiet_holding_moves")

    # Provenance, so a frozen config can be traced back to the run that produced it.
    fitted_on: str = "not fitted — first guesses, 12 Nov 2026"


DEFAULT = FindabilityConfig()
