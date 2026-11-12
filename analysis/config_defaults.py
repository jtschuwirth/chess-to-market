"""One config object for the whole pipeline.

Every default here is a **first guess, not a fitted value** (T-P03 fits them), except the two that
were measured:

- `latency_s = 10` — the edge is flat from 0 to 20 s and gone by 60 s (`research/latency-pnl.md`)
- `paper.max_hold_s = 120` — a timer beats both holding to settlement and converging once the spread
  is paid (`research/spread.md`)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .findability import DEFAULT as FINDABILITY_DEFAULT
from .findability import FindabilityConfig
from .paper import PaperConfig
from .triggers import DEFAULT as TRIGGER_DEFAULT
from .triggers import TriggerConfig


@dataclass(frozen=True)
class PipelineConfig:
    trigger: TriggerConfig = TRIGGER_DEFAULT
    findability: FindabilityConfig = FINDABILITY_DEFAULT
    paper: PaperConfig = field(default_factory=lambda: PaperConfig(max_hold_s=120.0))

    # Which depth's eval records drive decisions. The Rust emits several per position (12 and 16);
    # deciding on every one of them would fire the same event twice. None means "use them all".
    eval_depth: int | None = 16

    # Entry latency to model. 10 s sits inside the flat part of the decay curve.
    latency_s: float = 10.0

    # Mapping from colour to market leg. Required for paper positions, since nothing in the log says
    # which leg is white. Signals are still produced without it.
    white_leg: str | None = None
    black_leg: str | None = None

    # How the market resolved. Only needed for positions held to settlement; a timed exit never
    # reaches it. None disables position simulation entirely.
    resolved_leg: str | None = None
