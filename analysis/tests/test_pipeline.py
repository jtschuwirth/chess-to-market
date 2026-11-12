"""Pipeline tests, including the health diagnoses that make a misbehaving producer obvious."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from analysis.config_defaults import PipelineConfig
from analysis.observations import read
from analysis.paper import PaperConfig
from analysis.pipeline import build_tape, run
from analysis.synth import generate


def to_records(lines):
    p = Path(tempfile.mkdtemp()) / "l.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return read(str(p))[0]


def line(mono, source, **payload):
    return json.dumps({"mono_ms": mono, "wall_unix_ms": 1_764_000_000_000 + mono,
                       "payload": {"source": source, **payload}})


class TestHealthDiagnoses(unittest.TestCase):
    """The point of `health()`: tell "no signals" apart from "no data"."""

    def test_missing_engine_source_is_called_out(self):
        res = run(to_records([line(1, "ply", ply=1, san="e4")]))
        self.assertIn("no eval records", res.health.report())

    def test_evals_without_wdl_are_called_out(self):
        res = run(to_records([line(1, "eval", ply=1, depth=16, cp_white=-50)]))
        self.assertIn("without wdl_white", res.health.report())

    def test_missing_market_source_is_called_out(self):
        res = run(to_records([line(1, "ply", ply=1, san="e4")]))
        self.assertIn("no market records", res.health.report())

    def test_evals_without_lines_warn_about_findability(self):
        res = run(to_records([line(1, "eval", ply=1, depth=16, wdl_white=[100, 800, 100])]))
        self.assertIn("findability will abstain", res.health.report())

    def test_market_without_leg_warns(self):
        recs = to_records([line(1, "market", asset_id="x", event_type="book", best_ask=0.5)])
        self.assertIn("no `leg`", run(recs).health.report())

    def test_counts_every_source(self):
        res = run(to_records([
            line(1, "ply", ply=1, san="e4"),
            line(2, "note", message="hi"),
            line(3, "market", leg="a", event_type="book", best_ask=0.5),
        ]))
        self.assertEqual(res.health.counts, {"ply": 1, "note": 1, "market": 1})


class TestTape(unittest.TestCase):
    def test_touch_is_carried_forward(self):
        # A price_change on one side must not wipe out the other side's known price.
        tape = build_tape(to_records([
            line(1, "market", leg="a", event_type="book", best_bid=0.40, best_ask=0.44),
            line(2, "market", leg="a", event_type="price_change", best_ask=0.46),
        ]))
        self.assertEqual(tape[-1]["bid"], 0.40)
        self.assertEqual(tape[-1]["ask"], 0.46)

    def test_records_without_a_leg_are_skipped(self):
        self.assertEqual(build_tape(to_records([
            line(1, "market", asset_id="x", event_type="book", best_ask=0.5)])), [])


class TestSignals(unittest.TestCase):
    def setUp(self):
        self.records = to_records(generate())

    def test_reference_log_produces_exactly_one_signal(self):
        res = run(self.records)
        self.assertEqual(len(res.signals), 1)

    def test_the_signal_is_the_blunder(self):
        s = run(self.records).signals[0]
        self.assertEqual(s.ply, 5)
        self.assertEqual(s.san, "Rf2??")
        self.assertEqual(s.trigger.kind.value, "decisive")

    def test_findability_sees_the_capture(self):
        s = run(self.records).signals[0]
        self.assertGreater(s.findability, 0.85)
        self.assertIn("capture", s.findability_note)

    def test_repeated_evals_of_one_position_cannot_double_fire(self):
        """A property of the design, not of the cooldown.

        The log carries depth 12 and 16 for every ply, 1.5s apart. Both report the SAME state, and
        the trigger is a state *transition* — so the second one has nothing to transition from. The
        guard is structural, and it holds with the cooldown switched off entirely.
        """
        from analysis.triggers import TriggerConfig
        one = run(self.records)
        both = run(self.records, replace(PipelineConfig(), eval_depth=None))
        no_cooldown = run(self.records, replace(
            PipelineConfig(), eval_depth=None, trigger=TriggerConfig(cooldown_s=0)))
        self.assertEqual(len(one.signals), 1)
        self.assertEqual(len(both.signals), 1)
        self.assertEqual(len(no_cooldown.signals), 1)

    def test_shallow_depth_can_be_decided_on(self):
        # depth 12 lands in ~70ms against depth 16's ~1.6s, so it is a real option.
        res = run(self.records, replace(PipelineConfig(), eval_depth=12))
        self.assertEqual(len(res.signals), 1)
        self.assertEqual(res.signals[0].ply, 5)

    def test_no_positions_without_a_leg_mapping(self):
        res = run(self.records)
        self.assertEqual(res.positions, [])
        self.assertIsNone(res.signals[0].room)

    def test_positions_appear_once_legs_are_mapped(self):
        cfg = replace(PipelineConfig(), white_leg="ding", black_leg="gukesh",
                      resolved_leg="gukesh", paper=PaperConfig(max_hold_s=120))
        res = run(self.records, cfg)
        self.assertEqual(len(res.positions), 1)
        self.assertEqual(res.positions[0].leg, "gukesh")
        self.assertGreater(res.pnl_usd, 0)

    def test_fees_are_charged_when_a_rate_is_set(self):
        base = replace(PipelineConfig(), white_leg="ding", black_leg="gukesh", resolved_leg="gukesh")
        free = run(self.records, replace(base, paper=PaperConfig(max_hold_s=120)))
        paid = run(self.records, replace(base, paper=PaperConfig(max_hold_s=120, fee_rate=0.05)))
        self.assertEqual(free.fees_usd, 0)
        self.assertGreater(paid.fees_usd, 0)
        self.assertLess(paid.pnl_usd, free.pnl_usd)

    def test_confidence_is_room_times_findability(self):
        cfg = replace(PipelineConfig(), white_leg="ding", black_leg="gukesh")
        s = run(self.records, cfg).signals[0]
        self.assertAlmostEqual(s.confidence, (s.room or 0) * s.findability, places=9)

    def test_deterministic(self):
        a = run(self.records).signals[0]
        b = run(self.records).signals[0]
        self.assertEqual((a.ply, a.findability, a.trigger.kind), (b.ply, b.findability, b.trigger.kind))

    def test_empty_log_is_not_an_error(self):
        res = run([])
        self.assertEqual(res.signals, [])
        self.assertEqual(res.pnl_usd, 0)


if __name__ == "__main__":
    unittest.main()
