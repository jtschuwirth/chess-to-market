"""Cooldown tests — one event must not become three orders.

Game 8 went collapse -> decisive -> collapse inside nine seconds. In the backtest that counted one
repricing burst three times; live it would have placed three orders on one event.
"""

from __future__ import annotations

import unittest

from analysis.triggers import StateTracker, TriggerConfig, TriggerKind


class TestCooldown(unittest.TestCase):
    def test_game_8_oscillation_fires_once(self):
        # 39.Qd4 collapse, 39...Qg2 decisive, 40.Qf2 collapse — 12:48:40, :49, :49
        t = StateTracker(TriggerConfig(cooldown_s=120))
        seq = [
            (0.0, (0.90, 0.10, 0.00)),      # white winning, baseline
            (520.0, (0.23, 0.77, 0.00)),    # collapse — fires
            (529.0, (0.90, 0.10, 0.00)),    # decisive — suppressed
            (529.0, (0.45, 0.55, 0.00)),    # collapse — suppressed
        ]
        fired = [t.update(*p, ts=ts) for ts, p in seq]
        self.assertEqual(sum(1 for f in fired if f), 1)
        self.assertEqual(t.suppressed, 2)

    def test_state_still_advances_while_suppressed(self):
        # The tracker must not lose track of who is winning just because it stayed quiet.
        t = StateTracker(TriggerConfig(cooldown_s=120))
        t.update(0.90, 0.10, 0.00, ts=0)
        t.update(0.20, 0.75, 0.05, ts=10)      # collapse, fires
        t.update(0.95, 0.05, 0.00, ts=20)      # decisive, suppressed
        # Well after the cooldown, a genuine new transition must fire from the CURRENT state.
        trig = t.update(0.10, 0.85, 0.05, ts=500)
        self.assertIsNotNone(trig)
        self.assertIs(trig.kind, TriggerKind.COLLAPSE)

    def test_separate_events_still_both_fire(self):
        t = StateTracker(TriggerConfig(cooldown_s=120))
        t.update(0.20, 0.70, 0.10, ts=0)
        a = t.update(0.95, 0.05, 0.00, ts=100)
        b = t.update(0.20, 0.75, 0.05, ts=400)   # 300 s later, well past the cooldown
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)

    def test_cooldown_disabled_by_zero(self):
        t = StateTracker(TriggerConfig(cooldown_s=0))
        t.update(0.90, 0.10, 0.00, ts=0)
        self.assertIsNotNone(t.update(0.20, 0.75, 0.05, ts=1))
        self.assertIsNotNone(t.update(0.95, 0.05, 0.00, ts=2))

    def test_no_timestamp_means_no_cooldown(self):
        # Callers that do not supply ts get the old behaviour rather than a silent surprise.
        t = StateTracker(TriggerConfig(cooldown_s=120))
        t.update(0.90, 0.10, 0.00)
        self.assertIsNotNone(t.update(0.20, 0.75, 0.05))
        self.assertIsNotNone(t.update(0.95, 0.05, 0.00))

    def test_negative_cooldown_rejected(self):
        with self.assertRaises(ValueError):
            TriggerConfig(cooldown_s=-1)


if __name__ == "__main__":
    unittest.main()
