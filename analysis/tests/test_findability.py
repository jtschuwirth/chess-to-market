"""Findability tests, built from real measured positions.

The MultiPV lines below are actual Stockfish 19 output at depth 16 for the positions after the two
blunders in the WCC 2024 dataset, with win probabilities from `UCI_ShowWDL`.
"""

from __future__ import annotations

import unittest

from analysis.findability import Candidate, FindabilityConfig, findability

# WCC 2024 game 14, after 55. Rf2?? — FEN B7/8/4b3/4kp2/6p1/6P1/1r3R2/6K1 b - - 17 55
# Exactly one move wins, and it is a capture. The market repriced within 31 s — which is the whole
# reason `forcing_move` must pre-empt the holding count.
GAME_14 = [
    Candidate(san="Rxf2", uci="b2f2", win_prob=1.000, draw_prob=0.000, cp=667),
    Candidate(san="Rb1+", uci="b2b1", win_prob=0.036, draw_prob=0.940, cp=40),
    Candidate(san="Rb8", uci="b2b8", win_prob=0.020, draw_prob=0.950, cp=30),
    Candidate(san="Rb6", uci="b2b6", win_prob=0.016, draw_prob=0.950, cp=25),
]

# WCC 2024 game 12, after 38...f6?? — FEN 5rk1/1p1PR1p1/n2B1pbp/1q1P4/p7/P3Q1PP/1Pr2PBK/3R4 w - - 0 39
# Every one of the top four moves wins. Market was pinned at 1.00 and did not move.
GAME_12 = [
    Candidate(san="Rxg7+", uci="e7g7", win_prob=1.000, draw_prob=0.000, cp=1405),
    Candidate(san="Rd2", uci="d1d2", win_prob=1.000, draw_prob=0.000, cp=1074),
    Candidate(san="Rc1", uci="d1c1", win_prob=1.000, draw_prob=0.000, cp=1066),
    Candidate(san="Bf1", uci="g2f1", win_prob=1.000, draw_prob=0.000, cp=1040),
]

# Synthetic: a single QUIET move holds the win. The case the model is really for.
ONE_QUIET = [
    Candidate(san="Kg2", uci="h2g2", win_prob=0.97, draw_prob=0.03, cp=800),
    Candidate(san="Rb1", uci="d1b1", win_prob=0.10, draw_prob=0.85, cp=60),
    Candidate(san="Rc1", uci="d1c1", win_prob=0.08, draw_prob=0.87, cp=50),
    Candidate(san="Be4", uci="g2e4", win_prob=0.05, draw_prob=0.90, cp=30),
]

# Synthetic: several quiet moves hold. Hard to go wrong.
MANY_QUIET = [
    Candidate(san="Kg2", uci="h2g2", win_prob=0.97, draw_prob=0.03, cp=800),
    Candidate(san="Rb1", uci="d1b1", win_prob=0.95, draw_prob=0.05, cp=770),
    Candidate(san="Rc1", uci="d1c1", win_prob=0.94, draw_prob=0.06, cp=760),
    Candidate(san="Be4", uci="g2e4", win_prob=0.93, draw_prob=0.07, cp=745),
]

# A dead-drawn position: nobody is winning, so there is nothing to hold.
DRAWN = [
    Candidate(san="Kf3", uci="e3f3", win_prob=0.01, draw_prob=0.98, cp=12),
    Candidate(san="Kd3", uci="e3d3", win_prob=0.01, draw_prob=0.98, cp=10),
]


class TestForcingPreEmptsTheCount(unittest.TestCase):
    """A professional considers every capture and check, so the holding count is irrelevant there."""

    def test_forcing_move_supplies_the_base(self):
        f = findability(GAME_14)
        self.assertEqual([x.name for x in f.factors], ["forcing_move"])

    def test_holding_count_is_not_applied_to_a_forcing_move(self):
        self.assertNotIn("quiet_holding_moves", [x.name for x in findability(GAME_14).factors])

    def test_unique_forcing_move_still_scores_high(self):
        # The old model scored this 0.435 because only one move held. That was wrong: Rxf2 is a
        # winning capture and the market repriced it in 31 seconds.
        self.assertGreater(findability(GAME_14).score, 0.85)

    def test_unique_forcing_beats_unique_quiet(self):
        self.assertGreater(findability(GAME_14).score, findability(ONE_QUIET).score)

    def test_capture_plus_check_beats_capture_alone(self):
        self.assertGreater(findability(GAME_12).score, findability(GAME_14).score)

    def test_mate_is_nearly_always_found(self):
        mate = [Candidate(san="Rf8#", uci="b2b8", win_prob=1.0, draw_prob=0.0)] + GAME_14[1:]
        self.assertGreater(findability(mate).score, findability(GAME_14).score)


class TestQuietHoldingMoves(unittest.TestCase):
    def test_quiet_move_uses_the_holding_count(self):
        f = findability(ONE_QUIET)
        self.assertEqual([x.name for x in f.factors], ["quiet_holding_moves"])

    def test_single_holding_quiet_move_is_least_findable(self):
        f = findability(ONE_QUIET)
        self.assertIn("1 of 4 lines hold", f.factors[0].note)
        self.assertLess(f.score, 0.5)

    def test_many_holding_quiet_moves_score_higher(self):
        self.assertGreater(findability(MANY_QUIET).score, findability(ONE_QUIET).score)

    def test_saturated_count_is_flagged(self):
        self.assertIn("capped by MultiPV", findability(MANY_QUIET).factors[0].note)

    def test_note_reports_how_many_holders_are_quiet(self):
        self.assertIn("quiet", findability(MANY_QUIET).factors[0].note)

    def test_tolerance_is_tunable(self):
        # Alternatives that ARE winning, just further behind: tolerance decides whether they count.
        near_misses = [
            Candidate(san="Kg2", uci="h2g2", win_prob=0.97, draw_prob=0.03),
            Candidate(san="Rb1", uci="d1b1", win_prob=0.80, draw_prob=0.20),
            Candidate(san="Rc1", uci="d1c1", win_prob=0.78, draw_prob=0.22),
            Candidate(san="Be4", uci="g2e4", win_prob=0.76, draw_prob=0.24),
        ]
        loose = findability(near_misses, FindabilityConfig(hold_tolerance=0.99))
        tight = findability(near_misses, FindabilityConfig(hold_tolerance=0.01))
        self.assertGreater(loose.score, tight.score)

    def test_tolerance_cannot_rescue_losing_alternatives(self):
        # In ONE_QUIET the alternatives are not winning at all (win_prob 0.05-0.10), so no tolerance
        # makes them "hold". The winning guard, not the tolerance, is what excludes them.
        loose = findability(ONE_QUIET, FindabilityConfig(hold_tolerance=0.99))
        tight = findability(ONE_QUIET, FindabilityConfig(hold_tolerance=0.01))
        self.assertEqual(loose.score, tight.score)


class TestAbstention(unittest.TestCase):
    def test_drawn_position_abstains(self):
        f = findability(DRAWN)
        self.assertEqual(f.score, 0.5)
        self.assertEqual([x.name for x in f.factors], ["abstain"])

    def test_empty_input_abstains(self):
        self.assertEqual(findability([]).score, 0.5)

    def test_forcing_but_not_winning_abstains(self):
        # A capture in a dead-drawn position is not a findability question.
        drawn_capture = [Candidate(san="Kxf3", uci="e3f3", win_prob=0.01, draw_prob=0.98)] + DRAWN[1:]
        self.assertEqual([x.name for x in findability(drawn_capture).factors], ["abstain"])


class TestComposition(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(findability(GAME_14).score, findability(GAME_14).score)

    def test_score_stays_inside_the_clamp(self):
        cfg = FindabilityConfig()
        for case in (GAME_12, GAME_14, ONE_QUIET, MANY_QUIET, DRAWN):
            self.assertGreaterEqual(findability(case, cfg).score, cfg.floor)
            self.assertLessEqual(findability(case, cfg).score, cfg.ceiling)

    def test_unknown_heuristic_is_an_error(self):
        with self.assertRaises(KeyError):
            findability(GAME_14, FindabilityConfig(enabled=("no_such_heuristic",)))

    def test_explain_names_the_factor(self):
        self.assertIn("forcing_move", findability(GAME_14).explain())

    def test_only_one_base_is_ever_applied(self):
        for case in (GAME_12, GAME_14, ONE_QUIET, MANY_QUIET):
            names = [x.name for x in findability(case).factors]
            self.assertEqual(len([n for n in names if n in ("forcing_move", "quiet_holding_moves")]), 1)


if __name__ == "__main__":
    unittest.main()
