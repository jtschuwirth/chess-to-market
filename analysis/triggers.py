"""What counts as a trigger: a change in who is winning.

**No centipawn thresholds.** A fixed cp delta is the wrong trigger and the dataset showed why: of 19
swings detected at 100 cp, 5 were "the winning side winning more" in a game already decided, 2 were
fixed-depth horizon artifacts inside a forced sequence, and 9 were margin changes that left the
beneficiary still losing. Almost none of them were the event we care about.

The event we care about is the **tide turning**: level becomes winning, or one side's win becomes the
other's. That is what changes the distribution of *results*, which is the only thing a prediction
market prices.

States come from Stockfish's `wdl` output (`UCI_ShowWDL`), normalised to white, so everything here is
in probability space and directly comparable to a market price.

Pure: deterministic in the sequence of plies fed to it, with no clock, network or randomness. The
same code therefore runs over a backtest and a live log.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GameState(Enum):
    """Who is winning, as the engine sees it."""

    WHITE_WINNING = "white_winning"
    LEVEL = "level"
    BLACK_WINNING = "black_winning"

    @property
    def is_winning(self) -> bool:
        return self is not GameState.LEVEL


class TriggerKind(Enum):
    DECISIVE = "decisive"   # level -> someone winning: the game broke open
    FLIP = "flip"           # one side winning -> the other: the tide reversed outright
    COLLAPSE = "collapse"   # winning -> level: an advantage thrown away


@dataclass(frozen=True)
class Trigger:
    kind: TriggerKind
    before: GameState
    after: GameState
    p_white_win: float
    p_draw: float
    p_black_win: float

    def __str__(self) -> str:
        return f"{self.kind.value}: {self.before.value} -> {self.after.value}"


@dataclass(frozen=True)
class TriggerConfig:
    # A side is "winning" once its win probability reaches `enter`, and stops being winning only
    # when it falls below `exit`. Two thresholds rather than one, because a single threshold makes
    # the state flicker on every ply when the evaluation sits near it — and each flicker would be a
    # false trigger.
    enter: float = 0.70
    exit: float = 0.55

    # After emitting a trigger, suppress further ones for this many seconds.
    #
    # Not only a backtest concern. In game 8 the state went collapse -> decisive -> collapse inside
    # NINE seconds, which live would mean three orders placed on one event, and in the backtest meant
    # one repricing burst counted three times. The engine's verdict genuinely oscillates in sharp
    # positions; a cooldown is the cheapest honest answer. 0 disables it.
    cooldown_s: float = 120.0

    # Which transitions count. COLLAPSE is included because a thrown-away win is a real
    # result-distribution change and the market prices it through the draw leg — but it is the one
    # kind worth reconsidering, since it is the mirror of DECISIVE rather than a new event.
    kinds: tuple[TriggerKind, ...] = (
        TriggerKind.DECISIVE,
        TriggerKind.FLIP,
        TriggerKind.COLLAPSE,
    )

    def __post_init__(self) -> None:
        if not 0 < self.exit <= self.enter < 1:
            raise ValueError(f"need 0 < exit <= enter < 1, got exit={self.exit} enter={self.enter}")
        if self.cooldown_s < 0:
            raise ValueError(f"cooldown_s must be >= 0, got {self.cooldown_s}")


DEFAULT = TriggerConfig()


def classify(p_white_win: float, p_black_win: float, cfg: TriggerConfig | None = None) -> GameState:
    """State with no history — used for the first ply, and for tests."""
    cfg = cfg or DEFAULT
    if p_white_win >= cfg.enter:
        return GameState.WHITE_WINNING
    if p_black_win >= cfg.enter:
        return GameState.BLACK_WINNING
    return GameState.LEVEL


def _kind(before: GameState, after: GameState) -> TriggerKind | None:
    if before is after:
        return None
    if before is GameState.LEVEL:
        return TriggerKind.DECISIVE
    if after is GameState.LEVEL:
        return TriggerKind.COLLAPSE
    return TriggerKind.FLIP


class StateTracker:
    """Feed plies in order; get a `Trigger` on the plies where the tide turned.

    Stateful by necessity — hysteresis needs to know where it has been — but still a pure function
    of the sequence: the same plies in the same order always give the same triggers.
    """

    def __init__(self, cfg: TriggerConfig | None = None) -> None:
        self.cfg = cfg or DEFAULT
        self.state: GameState | None = None
        self.last_trigger_ts: float | None = None
        self.suppressed = 0

    def update(
        self,
        p_white_win: float,
        p_draw: float,
        p_black_win: float,
        ts: float | None = None,
    ) -> Trigger | None:
        """Feed one ply. Pass `ts` (seconds) to enable the cooldown.

        The state still advances while suppressed — only the emission is withheld — so the tracker
        never loses track of who is winning.
        """
        cfg = self.cfg
        if self.state is None:
            # First ply establishes a baseline and can never be a trigger: there is nothing to
            # have changed from.
            self.state = classify(p_white_win, p_black_win, cfg)
            return None

        prev = self.state
        if prev is GameState.WHITE_WINNING:
            if p_white_win >= cfg.exit:
                new = GameState.WHITE_WINNING          # still winning, held by hysteresis
            elif p_black_win >= cfg.enter:
                new = GameState.BLACK_WINNING
            else:
                new = GameState.LEVEL
        elif prev is GameState.BLACK_WINNING:
            if p_black_win >= cfg.exit:
                new = GameState.BLACK_WINNING
            elif p_white_win >= cfg.enter:
                new = GameState.WHITE_WINNING
            else:
                new = GameState.LEVEL
        else:
            new = classify(p_white_win, p_black_win, cfg)

        self.state = new
        kind = _kind(prev, new)
        if kind is None or kind not in cfg.kinds:
            return None

        if (
            cfg.cooldown_s
            and ts is not None
            and self.last_trigger_ts is not None
            and ts - self.last_trigger_ts < cfg.cooldown_s
        ):
            self.suppressed += 1
            return None

        if ts is not None:
            self.last_trigger_ts = ts
        return Trigger(kind, prev, new, p_white_win, p_draw, p_black_win)
