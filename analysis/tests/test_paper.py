"""Paper-position tests. No orders are placed anywhere in this package."""

from __future__ import annotations

import unittest

from analysis.paper import PaperConfig, choose_leg, implied_legs, simulate

# A simple tape: trigger at t=1000, ding underpriced at 0.40 while the engine says 0.95.
TAPE = [
    {"leg": "ding", "t": 900, "p": 0.40},
    {"leg": "draw", "t": 900, "p": 0.55},
    {"leg": "gukesh", "t": 900, "p": 0.05},
    {"leg": "ding", "t": 1005, "p": 0.45},
    {"leg": "ding", "t": 1030, "p": 0.60},
    {"leg": "ding", "t": 1100, "p": 0.80},
    {"leg": "ding", "t": 1200, "p": 0.30},
]
IMPLIED = {"ding": 0.95, "draw": 0.04, "gukesh": 0.01}


class TestLegChoice(unittest.TestCase):
    def test_picks_the_underpriced_leg(self):
        leg, edge = choose_leg(IMPLIED, {"ding": 0.40, "draw": 0.55, "gukesh": 0.05}, PaperConfig())
        self.assertEqual(leg, "ding")
        self.assertAlmostEqual(edge, 0.55)

    def test_no_bet_without_edge(self):
        market = {"ding": 0.94, "draw": 0.04, "gukesh": 0.02}
        self.assertIsNone(choose_leg(IMPLIED, market, PaperConfig()))

    def test_min_edge_is_configurable(self):
        market = {"ding": 0.90, "draw": 0.05, "gukesh": 0.05}
        self.assertIsNone(choose_leg(IMPLIED, market, PaperConfig(min_edge=0.20)))
        self.assertIsNotNone(choose_leg(IMPLIED, market, PaperConfig(min_edge=0.02)))

    def test_implied_legs_maps_colours_to_players(self):
        got = implied_legs(0.9, 0.08, 0.02, "ding", "gukesh")
        self.assertEqual(got, {"ding": 0.9, "draw": 0.08, "gukesh": 0.02})


class TestFill(unittest.TestCase):
    def test_entry_is_the_first_price_at_or_after_the_intended_time(self):
        t = simulate(1000, 0, IMPLIED, TAPE, "ding")
        self.assertEqual(t.entry_ts, 1005)
        self.assertEqual(t.entry_price, 0.45)

    def test_latency_moves_the_fill_later_and_worse(self):
        fast = simulate(1000, 0, IMPLIED, TAPE, "ding")
        slow = simulate(1000, 60, IMPLIED, TAPE, "ding")
        self.assertEqual(slow.entry_price, 0.80)
        self.assertGreater(fast.pnl_usd, slow.pnl_usd)

    def test_no_fill_inside_the_horizon_is_no_trade(self):
        self.assertIsNone(simulate(1000, 0, IMPLIED, TAPE, "ding", PaperConfig(fill_horizon_s=1)))

    def test_no_prior_price_is_no_trade(self):
        self.assertIsNone(simulate(0, 0, IMPLIED, TAPE, "ding"))


class TestPnl(unittest.TestCase):
    def test_winning_position_pays_out(self):
        t = simulate(1000, 0, IMPLIED, TAPE, "ding")
        # $10 at 0.45 buys 22.22 shares; resolution at 1.0 gives 22.22 * 0.55
        self.assertAlmostEqual(t.shares, 10 / 0.45)
        self.assertAlmostEqual(t.pnl_usd, (10 / 0.45) * (1 - 0.45), places=6)
        self.assertEqual(t.exit_reason, "resolution")

    def test_losing_position_loses_the_stake(self):
        t = simulate(1000, 0, IMPLIED, TAPE, "gukesh")
        self.assertAlmostEqual(t.pnl_usd, -10.0, places=6)

    def test_stop_loss_exits_early(self):
        t = simulate(1000, 0, IMPLIED, TAPE, "ding", PaperConfig(stop_loss=0.10))
        self.assertEqual(t.exit_reason, "stop_loss")
        self.assertEqual(t.exit_price, 0.30)
        self.assertLess(t.pnl_usd, 0)

    def test_stop_loss_can_beat_holding_a_loser(self):
        losing_tape = TAPE + [{"leg": "ding", "t": 1300, "p": 0.10}]
        held = simulate(1000, 0, IMPLIED, losing_tape, "gukesh")
        stopped = simulate(1000, 0, IMPLIED, losing_tape, "gukesh", PaperConfig(stop_loss=0.10))
        self.assertGreater(stopped.pnl_usd, held.pnl_usd)

    def test_take_profit_exits_early(self):
        t = simulate(1000, 0, IMPLIED, TAPE, "ding", PaperConfig(take_profit=0.30))
        self.assertEqual(t.exit_reason, "take_profit")
        self.assertEqual(t.exit_price, 0.80)

    def test_stake_scales_pnl_linearly(self):
        a = simulate(1000, 0, IMPLIED, TAPE, "ding", PaperConfig(notional_usd=10))
        b = simulate(1000, 0, IMPLIED, TAPE, "ding", PaperConfig(notional_usd=20))
        self.assertAlmostEqual(b.pnl_usd, 2 * a.pnl_usd, places=6)

    def test_deterministic(self):
        self.assertEqual(
            simulate(1000, 0, IMPLIED, TAPE, "ding").pnl_usd,
            simulate(1000, 0, IMPLIED, TAPE, "ding").pnl_usd,
        )


if __name__ == "__main__":
    unittest.main()


class TestConvergenceExit(unittest.TestCase):
    """The exit that matches the thesis: take the money when the market catches up."""

    def test_converged_exit_fires_when_market_reaches_the_engine(self):
        from analysis.paper import PaperConfig as C
        t = simulate(1000, 0, IMPLIED, TAPE, "gukesh", C(converge_margin=0.20))
        # engine implies ding 0.95, so target is 0.75; the tape reaches 0.80
        self.assertEqual(t.exit_reason, "converged")
        self.assertEqual(t.exit_price, 0.80)

    def test_convergence_saves_a_position_the_engine_got_wrong(self):
        from analysis.paper import PaperConfig as C
        # "gukesh" wins, so a ding position is a total loss if held to resolution...
        held = simulate(1000, 0, IMPLIED, TAPE, "gukesh")
        self.assertAlmostEqual(held.pnl_usd, -10.0, places=6)
        # ...but the market repriced on the way, so converging out is profitable.
        conv = simulate(1000, 0, IMPLIED, TAPE, "gukesh", C(converge_margin=0.20))
        self.assertGreater(conv.pnl_usd, 0)

    def test_time_exit_closes_at_the_first_price_after_the_deadline(self):
        from analysis.paper import PaperConfig as C
        t = simulate(1000, 0, IMPLIED, TAPE, "ding", C(max_hold_s=50))
        self.assertEqual(t.exit_reason, "time_exit")
        self.assertEqual(t.exit_ts, 1100)

    def test_stop_loss_takes_priority_over_convergence(self):
        from analysis.paper import PaperConfig as C
        falling = [
            {"leg": "ding", "t": 900, "p": 0.40}, {"leg": "draw", "t": 900, "p": 0.55},
            {"leg": "gukesh", "t": 900, "p": 0.05},
            {"leg": "ding", "t": 1005, "p": 0.45},
            {"leg": "ding", "t": 1010, "p": 0.20},
            {"leg": "ding", "t": 1100, "p": 0.90},
        ]
        t = simulate(1000, 0, IMPLIED, falling, "ding", C(stop_loss=0.10, converge_margin=0.20))
        self.assertEqual(t.exit_reason, "stop_loss")


class TestFees(unittest.TestCase):
    """fee = shares x rate x p x (1 - p), takers only, charged on both fills."""

    def test_no_fee_by_default(self):
        self.assertEqual(simulate(1000, 0, IMPLIED, TAPE, "ding").fees_usd, 0.0)

    def test_entry_fee_matches_the_formula(self):
        from analysis.paper import PaperConfig as C
        t = simulate(1000, 0, IMPLIED, TAPE, "ding", C(fee_rate=0.05))
        shares = 10 / 0.45
        self.assertAlmostEqual(t.fees_usd, shares * 0.05 * 0.45 * 0.55, places=6)

    def test_settlement_pays_only_one_fee(self):
        from analysis.paper import PaperConfig as C
        t = simulate(1000, 0, IMPLIED, TAPE, "ding", C(fee_rate=0.05))
        self.assertEqual(t.exit_reason, "resolution")
        shares = 10 / 0.45
        self.assertAlmostEqual(t.fees_usd, shares * 0.05 * 0.45 * 0.55, places=6)

    def test_a_timed_exit_pays_twice(self):
        from analysis.paper import PaperConfig as C
        once = simulate(1000, 0, IMPLIED, TAPE, "ding", C(fee_rate=0.05))
        twice = simulate(1000, 0, IMPLIED, TAPE, "ding", C(fee_rate=0.05, max_hold_s=50))
        self.assertGreater(twice.fees_usd, once.fees_usd)

    def test_fee_is_a_bigger_share_of_notional_on_a_cheap_leg(self):
        from analysis.paper import PaperConfig as C
        cheap = [{"leg": "ding", "t": 900, "p": 0.40}, {"leg": "draw", "t": 900, "p": 0.55},
                 {"leg": "gukesh", "t": 900, "p": 0.05},
                 {"leg": "ding", "t": 1005, "p": 0.10}]
        dear = [{"leg": "ding", "t": 900, "p": 0.40}, {"leg": "draw", "t": 900, "p": 0.55},
                {"leg": "gukesh", "t": 900, "p": 0.05},
                {"leg": "ding", "t": 1005, "p": 0.90}]
        c = simulate(1000, 0, IMPLIED, cheap, "ding", C(fee_rate=0.05))
        d = simulate(1000, 0, IMPLIED, dear, "ding", C(fee_rate=0.05))
        # same $10 notional, but the cheap leg costs far more in fees
        self.assertGreater(c.fees_usd, 4 * d.fees_usd)

    def test_gross_pnl_is_recoverable(self):
        from analysis.paper import PaperConfig as C
        t = simulate(1000, 0, IMPLIED, TAPE, "ding", C(fee_rate=0.05))
        self.assertAlmostEqual(t.pnl_gross_usd, t.pnl_usd + t.fees_usd, places=9)
