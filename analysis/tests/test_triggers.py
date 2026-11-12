"""Trigger tests — state transitions, not centipawn deltas."""

from __future__ import annotations

import unittest

from analysis.triggers import (
    GameState,
    StateTracker,
    TriggerConfig,
    TriggerKind,
    classify,
)


def run(seq, cfg=None):
    """Feed (p_white_win, p_draw, p_black_win) triples; return the triggers that fired."""
    t = StateTracker(cfg)
    return [(i, trig) for i, p in enumerate(seq) if (trig := t.update(*p)) is not None]


class TestClassify(unittest.TestCase):
    def test_white_winning(self):
        self.assertIs(classify(0.80, 0.05), GameState.WHITE_WINNING)

    def test_black_winning(self):
        self.assertIs(classify(0.05, 0.85), GameState.BLACK_WINNING)

    def test_level(self):
        self.assertIs(classify(0.20, 0.10), GameState.LEVEL)

    def test_drawish_is_level_even_with_an_edge(self):
        self.assertIs(classify(0.45, 0.05), GameState.LEVEL)


class TestTheEventWeCareAbout(unittest.TestCase):
    def test_level_to_winning_is_decisive(self):
        fired = run([(0.20, 0.70, 0.10), (0.05, 0.10, 0.90)])
        self.assertEqual(len(fired), 1)
        self.assertIs(fired[0][1].kind, TriggerKind.DECISIVE)

    def test_flip_is_detected(self):
        fired = run([(0.90, 0.05, 0.05), (0.02, 0.03, 0.95)])
        self.assertIs(fired[0][1].kind, TriggerKind.FLIP)

    def test_thrown_away_win_is_a_collapse(self):
        fired = run([(0.90, 0.05, 0.05), (0.20, 0.75, 0.05)])
        self.assertIs(fired[0][1].kind, TriggerKind.COLLAPSE)

    def test_first_ply_never_triggers(self):
        self.assertEqual(run([(0.95, 0.03, 0.02)]), [])


class TestNoiseThatMustNotTrigger(unittest.TestCase):
    """Everything the 100 cp threshold caught that we do not want."""

    def test_winning_more_is_not_a_trigger(self):
        # Game 12: Ding +157 -> +1512 cp, winning throughout. Five "swings", no tide change.
        fired = run([
            (0.75, 0.20, 0.05), (0.88, 0.10, 0.02), (0.95, 0.04, 0.01),
            (0.99, 0.01, 0.00), (1.00, 0.00, 0.00),
        ])
        self.assertEqual(fired, [])

    def test_losing_and_still_losing_is_not_a_trigger(self):
        # "If you are losing and the swing keeps you losing, that is no valid trigger."
        fired = run([(0.02, 0.08, 0.90), (0.10, 0.15, 0.75), (0.05, 0.10, 0.85)])
        self.assertEqual(fired, [])

    def test_level_wobble_is_not_a_trigger(self):
        fired = run([(0.20, 0.70, 0.10), (0.35, 0.55, 0.10), (0.15, 0.75, 0.10)])
        self.assertEqual(fired, [])

    def test_forced_sequence_fires_once_not_at_every_step(self):
        # Game 14's cascade: 55.Rf2?? then Rxf2, Bd5, Bxd5, Kxd5 — one event, four plies.
        fired = run([
            (0.30, 0.55, 0.15),   # level, before the blunder
            (0.00, 0.00, 1.00),   # 55.Rf2?? — the tide turns
            (0.00, 0.00, 1.00),   # the rest of the forced line
            (0.00, 0.00, 1.00),
            (0.00, 0.00, 1.00),
        ])
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0], 1)


class TestHysteresis(unittest.TestCase):
    def test_oscillation_around_the_threshold_fires_once(self):
        # Without hysteresis a single threshold at 0.70 would fire on every crossing.
        cfg = TriggerConfig(enter=0.70, exit=0.55)
        fired = run([
            (0.60, 0.35, 0.05),
            (0.72, 0.25, 0.03),   # enters WHITE_WINNING — one trigger
            (0.68, 0.28, 0.04),   # dips below enter but above exit: still winning
            (0.71, 0.25, 0.04),
            (0.69, 0.27, 0.04),
        ], cfg)
        self.assertEqual(len(fired), 1)

    def test_falling_below_exit_does_collapse(self):
        cfg = TriggerConfig(enter=0.70, exit=0.55)
        fired = run([(0.60, 0.35, 0.05), (0.75, 0.22, 0.03), (0.40, 0.55, 0.05)], cfg)
        self.assertEqual([t.kind for _, t in fired], [TriggerKind.DECISIVE, TriggerKind.COLLAPSE])

    def test_invalid_thresholds_rejected(self):
        with self.assertRaises(ValueError):
            TriggerConfig(enter=0.5, exit=0.8)


class TestConfigurability(unittest.TestCase):
    def test_collapse_can_be_switched_off(self):
        cfg = TriggerConfig(kinds=(TriggerKind.DECISIVE, TriggerKind.FLIP))
        fired = run([(0.90, 0.05, 0.05), (0.20, 0.75, 0.05)], cfg)
        self.assertEqual(fired, [])

    def test_threshold_changes_what_counts_as_winning(self):
        seq = [(0.20, 0.70, 0.10), (0.65, 0.30, 0.05)]
        self.assertEqual(len(run(seq, TriggerConfig(enter=0.60, exit=0.50))), 1)
        self.assertEqual(len(run(seq, TriggerConfig(enter=0.80, exit=0.60))), 0)

    def test_deterministic(self):
        seq = [(0.20, 0.70, 0.10), (0.05, 0.10, 0.90), (0.50, 0.40, 0.10)]
        self.assertEqual([t.kind for _, t in run(seq)], [t.kind for _, t in run(seq)])


if __name__ == "__main__":
    unittest.main()
