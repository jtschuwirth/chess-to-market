"""Paper positions: execution-latency analysis for a public signal.

**This is an algorithmic trading model, not a wagering one.** We take no view on who wins the chess
game. The claim is narrower and purely about microstructure: a public signal (the engine's verdict on
a move that has already been played) reaches us before the market has repriced, and we capture the
convergence. Direction comes from the signal; the position is closed on a timer, well before the
game resolves.

That framing is why `hold to resolution` is not a strategy here — holding to settlement means taking
outcome risk we never claimed to price, and it is the one exit that requires the engine to be *right*
rather than merely *early*.

**Places no orders. Holds no keys. Touches no funds. Signs nothing.** This computes what a position
*would* have done from recorded prices, and has no ability to act.

The question it answers: given a signal at time T, if our order lands at T + latency, what does the
position return? Sweeping latency is the point — it measures the decay of the edge against execution
speed, which is the design requirement for everything upstream.

## Two-sided pricing

Tape records may carry `ask` and `bid` instead of a single `p`. When they do, **entry pays the ask and
every exit receives the bid**, so the spread is a real cost in the figures rather than an unmodelled
one.

This matters because of how Polymarket actually works: a position never has to wait for a buyer.
YES + NO redeems for $1, so a YES holder can always exit by **buying NO at its ask**, which is
economically selling YES at `1 - NO_ask` — exactly the YES bid. The exit is therefore available
whenever the complement is offered; what it costs is the spread, not liquidity.

## Fees

Polymarket's documented formula, takers only — **makers pay nothing and can earn rebates**:

    fee = shares x fee_rate x p x (1 - p)

Two consequences that matter here.

**Fee as a share of notional is `fee_rate x (1 - p)`**, because notional is `shares x p`. So a cheap
leg is expensive: at p = 0.11 a 0.05 rate costs **4.5 % of notional**, while at p = 0.95 it costs
0.25 %. This strategy buys underpriced legs, which is precisely the expensive end.

**We are a taker on both sides.** Entry lifts the ask; the exit either hits the bid or lifts the
complement's ask. Either way it is a taker fill, so the fee lands twice:

    total = shares x fee_rate x [p_in(1 - p_in) + p_out(1 - p_out)]

The two exit routes cost the same, since `p(1-p)` is symmetric and `bid = 1 - NO_ask`.

Fees began on Polymarket in January 2026 and reached most categories on 30 March 2026, so the
Dec-2024 backtest period had **none** — a live run does. `fee_rate` defaults to 0 to keep historical
figures honest; set it to model a live run.

## What is deliberately not modelled
- **Depth.** We fill at a price someone else actually traded at, which assumes our order could have
  taken that price for our full size. For $10 that is plausible and still optimistic.
- **Our own market impact.** None.

Every one of those makes the figures flattering. They are an upper bound on a hypothetical, not a
forecast. Pure and deterministic, so the same code runs over a backtest and a live log.
"""

from __future__ import annotations

from dataclasses import dataclass


def _px(rec: dict, side: str) -> float | None:
    """Price for one side of a tape record.

    A record carrying explicit `ask`/`bid` uses them. A record with only `p` is a single mid-ish
    print, used as both sides — which understates costs, so spread-aware tapes are preferred.
    """
    v = rec.get(side)
    if v is not None:
        return v
    return rec.get("p")


@dataclass(frozen=True)
class PaperConfig:
    notional_usd: float = 10.0
    # Taker fee rate in the formula above. 0 reproduces the pre-2026 fee-free regime, which is what
    # the historical tape actually had. Posting a resting order instead would pay 0 and possibly earn
    # a rebate — but a latency strategy has to take, so we pay.
    fee_rate: float = 0.0
    # Give up if no observable price for the chosen leg appears within this long after the intended
    # entry time. A fill we never saw is not a fill.
    fill_horizon_s: float = 600.0
    # Minimum gap between the engine's implied probability and the market price before betting.
    # Below this there is nothing to trade, whatever the engine says.
    min_edge: float = 0.10
    # Exit if the price falls this far below entry. None holds to resolution.
    stop_loss: float | None = None
    # Exit if the price rises this far above entry. None holds to resolution.
    take_profit: float | None = None
    # Exit once the market has repriced to within this margin of the engine's implied probability.
    #
    # This is the exit that matches the actual thesis. The edge measured in research/latency-pnl.md
    # is a LATENCY edge: we are in before the market moves. If that is what we are selling, we
    # should take the money when the market catches up, not carry the game's result risk for
    # another two hours. It also sidesteps the engine's calibration problem — Stockfish was wrong
    # about the eventual result in 3 of 8 cases, and a position closed on convergence does not care.
    converge_margin: float | None = None
    # Hard time exit: close at the first observable price after this long, whatever is happening.
    max_hold_s: float | None = None


@dataclass(frozen=True)
class PaperTrade:
    leg: str
    edge: float
    latency_s: float
    entry_ts: float
    entry_price: float
    shares: float
    exit_ts: float | None
    exit_price: float
    exit_reason: str
    pnl_usd: float
    fees_usd: float = 0.0

    @property
    def pnl_gross_usd(self) -> float:
        return self.pnl_usd + self.fees_usd

    @property
    def profitable(self) -> bool:
        return self.pnl_usd > 0

    @property
    def pnl_bps(self) -> float:
        """P&L in basis points of notional — the comparable unit across position sizes."""
        return 10_000 * self.pnl_usd / (self.shares * self.entry_price)


def implied_legs(
    p_white_win: float, p_draw: float, p_black_win: float, white_player: str, black_player: str
) -> dict[str, float]:
    """Engine WDL (already normalised to white) mapped onto the three market legs."""
    return {white_player: p_white_win, "draw": p_draw, black_player: p_black_win}


def choose_leg(
    implied: dict[str, float], market: dict[str, float], cfg: PaperConfig
) -> tuple[str, float] | None:
    """Buy the leg the engine thinks is cheapest relative to the market.

    Only a positive gap is tradeable here: we can buy a leg we think is underpriced, but selling
    short is a different instrument and is out of scope. Returns None when nothing clears `min_edge`.
    """
    best: tuple[str, float] | None = None
    for leg, p_eval in implied.items():
        if leg not in market:
            continue
        edge = p_eval - market[leg]
        if edge >= cfg.min_edge and (best is None or edge > best[1]):
            best = (leg, edge)
    return best


def simulate(
    trigger_ts: float,
    latency_s: float,
    implied: dict[str, float],
    trades: list[dict],
    winning_leg: str,
    cfg: PaperConfig | None = None,
) -> PaperTrade | None:
    """One hypothetical position.

    `trades` is the recorded tape: dicts with `leg`, `t`, `p` (outcome-normalised price), sorted by
    time. `winning_leg` is how the market actually resolved.
    """
    cfg = cfg or PaperConfig()

    # What the market showed at trigger time. We would be buying, so compare against the ask.
    market_now: dict[str, float] = {}
    for r in trades:
        if r["t"] <= trigger_ts:
            ask = r.get("ask")
            if ask is not None:
                market_now[r["leg"]] = ask
            elif "p" in r:
                market_now[r["leg"]] = r["p"]
    if not market_now:
        return None

    pick = choose_leg(implied, market_now, cfg)
    if pick is None:
        return None
    leg, edge = pick

    # Fill at the first price we actually observed at or after the intended entry time.
    want_ts = trigger_ts + latency_s
    entry = next(
        (r for r in trades
         if r["leg"] == leg and r["t"] >= want_ts and r["t"] <= want_ts + cfg.fill_horizon_s
         and _px(r, "ask") is not None),
        None,
    )
    if entry is None:
        return None
    entry_price = _px(entry, "ask")
    if entry_price is None or entry_price <= 0 or entry_price >= 1:
        return None  # nothing to buy at 0 or 1

    shares = cfg.notional_usd / entry_price

    target = None
    if cfg.converge_margin is not None:
        target = implied[leg] - cfg.converge_margin

    exit_price, exit_ts, reason = None, None, "resolution"
    for r in trades:
        if r["leg"] != leg or r["t"] <= entry["t"]:
            continue
        # Exiting means selling, or equivalently buying the complement: either way we receive the bid.
        bid = _px(r, "bid")
        if bid is None:
            continue
        if cfg.stop_loss is not None and bid <= entry_price - cfg.stop_loss:
            exit_price, exit_ts, reason = bid, r["t"], "stop_loss"
            break
        if cfg.take_profit is not None and bid >= entry_price + cfg.take_profit:
            exit_price, exit_ts, reason = bid, r["t"], "take_profit"
            break
        if target is not None and bid >= target:
            exit_price, exit_ts, reason = bid, r["t"], "converged"
            break
        if cfg.max_hold_s is not None and r["t"] >= entry["t"] + cfg.max_hold_s:
            exit_price, exit_ts, reason = bid, r["t"], "time_exit"
            break
    if exit_price is None:
        exit_price = 1.0 if leg == winning_leg else 0.0

    # Taker fee on both fills. Settlement is a redemption, not a trade, so it carries no taker fee.
    fee = shares * cfg.fee_rate * entry_price * (1 - entry_price)
    if reason != "resolution":
        fee += shares * cfg.fee_rate * exit_price * (1 - exit_price)

    return PaperTrade(
        leg=leg, edge=edge, latency_s=latency_s,
        entry_ts=entry["t"], entry_price=entry_price, shares=shares,
        exit_ts=exit_ts, exit_price=exit_price, exit_reason=reason,
        pnl_usd=shares * (exit_price - entry_price) - fee,
        fees_usd=fee,
    )
