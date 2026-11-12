"""Human findability — the tweakable layer between engine truth and market output.

The engine says what the position *is*. The market says what people *think*. This module models the
gap: **would a human actually find this?**

    engine truth  ──►  findability  ──►  expected market move  ──►  measure against reality
    (WDL, MultiPV)      (this module)      (signal confidence)

Why it exists: a 6-pawn evaluation swing whose refutation only works via one hard-to-see move is not
the same event as one refuted by an obvious capture, and the market treats them differently. Without
this layer every large swing looks alike, which is how you manufacture false signals.

Usage:

    from analysis.findability import Candidate, findability

    f = findability([
        Candidate(san="Rxf2", uci="b2f2", win_prob=1.00, draw_prob=0.00),
        Candidate(san="Rb1+", uci="b2b1", win_prob=0.03, draw_prob=0.55),
        Candidate(san="Rb6",  uci="b2b6", win_prob=0.02, draw_prob=0.56),
        Candidate(san="Rb4",  uci="b2b4", win_prob=0.01, draw_prob=0.57),
    ])
    print(f.explain())

Tuning lives in `config.py`; heuristics in `heuristics.py`. Adding a heuristic is one function plus
one registry entry.
"""

from .config import DEFAULT, FindabilityConfig
from .heuristics import REGISTRY
from .model import Candidate, Factor, Findability
from .score import findability

__all__ = [
    "Candidate",
    "Factor",
    "Findability",
    "FindabilityConfig",
    "DEFAULT",
    "REGISTRY",
    "findability",
]
