"""Log parsing: tolerant of additions, strict about what it uses."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from analysis.observations import KNOWN, check_ordering, parse_line, read, tail


def line(mono, source, **payload):
    return json.dumps({"mono_ms": mono, "wall_unix_ms": 1_764_000_000_000 + mono,
                       "payload": {"source": source, **payload}})


class TestParseLine(unittest.TestCase):
    def test_valid_record(self):
        rec, problem = parse_line(line(10, "ply", ply=1, san="e4", uci="e2e4"))
        self.assertIsNone(problem)
        self.assertEqual(rec.source, "ply")
        self.assertEqual(rec.ply, 1)

    def test_extra_fields_are_fine(self):
        # The Rust must be free to add fields mid-match without breaking the analysis.
        rec, problem = parse_line(line(10, "ply", ply=1, san="e4", something_new=42))
        self.assertIsNone(problem)
        self.assertEqual(rec.payload["something_new"], 42)

    def test_bad_json_is_reported_with_its_line(self):
        rec, problem = parse_line("not json", 7)
        self.assertIsNone(rec)
        self.assertIn("line 7", problem)

    def test_missing_payload(self):
        rec, problem = parse_line(json.dumps({"mono_ms": 1, "wall_unix_ms": 2}), 3)
        self.assertIsNone(rec)
        self.assertIn("payload", problem)

    def test_missing_clock_is_fatal_for_the_record(self):
        rec, problem = parse_line(json.dumps({"mono_ms": 1, "payload": {"source": "note"}}), 4)
        self.assertIsNone(rec)
        self.assertIn("wall_unix_ms", problem)

    def test_required_field_is_reported_but_record_kept(self):
        # A ply with no san is a hole in a measurement, so it is reported rather than dropped.
        rec, problem = parse_line(line(10, "ply", ply=1), 5)
        self.assertIsNotNone(rec)
        self.assertIn("missing san", problem)

    def test_eval_requires_wdl_white(self):
        _, problem = parse_line(line(10, "eval", ply=1, depth=16, cp_white=-50), 6)
        self.assertIn("wdl_white", problem)

    def test_wdl_is_converted_to_probabilities(self):
        rec, _ = parse_line(line(10, "eval", ply=1, wdl_white=[250, 700, 50]))
        self.assertEqual(rec.wdl_white(), (0.25, 0.70, 0.05))

    def test_market_requires_a_leg(self):
        _, problem = parse_line(line(10, "market", asset_id="x", event_type="book"), 9)
        self.assertIn("leg", problem)


class TestRead(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, lines):
        p = self.dir / "log.jsonl"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)

    def test_blank_lines_skipped(self):
        p = self.write([line(1, "note", message="a"), "", line(2, "note", message="b")])
        recs, rep = read(p)
        self.assertEqual(len(recs), 2)
        self.assertEqual(rep.total, 2)

    def test_unknown_source_counted_not_fatal(self):
        p = self.write([line(1, "note", message="a"), line(2, "future_thing", x=1)])
        recs, rep = read(p)
        self.assertEqual(len(recs), 1)
        self.assertEqual(rep.unknown_source, {"future_thing": 1})
        self.assertTrue(rep.ok())

    def test_truncated_last_line_is_reported(self):
        p = self.dir / "trunc.jsonl"
        p.write_text(line(1, "note", message="a") + "\n{\"mono_ms\": 2, \"payl", encoding="utf-8")
        _, rep = read(str(p))
        self.assertFalse(rep.ok())

    def test_tail_resumes_from_a_line(self):
        p = self.write([line(i, "note", message=str(i)) for i in range(5)])
        self.assertEqual(len(list(tail(p, from_line=3))), 2)

    def test_known_sources_are_the_documented_five(self):
        self.assertEqual(KNOWN, {"ply", "market", "eval", "signal", "note"})


class TestOrdering(unittest.TestCase):
    def test_monotonic_clock_going_backwards_is_caught(self):
        recs, _ = read_from([line(10, "note", message="a"), line(5, "note", message="b")])
        self.assertTrue(any("backwards" in p for p in check_ordering(recs)))

    def test_repeated_ply_is_caught(self):
        recs, _ = read_from([
            line(10, "ply", game_id="g", ply=5, san="a"),
            line(20, "ply", game_id="g", ply=5, san="b"),
        ])
        self.assertTrue(any("not after" in p for p in check_ordering(recs)))

    def test_separate_games_do_not_interfere(self):
        recs, _ = read_from([
            line(10, "ply", game_id="g1", ply=5, san="a"),
            line(20, "ply", game_id="g2", ply=1, san="b"),
        ])
        self.assertEqual(check_ordering(recs), [])


def read_from(lines):
    d = Path(tempfile.mkdtemp()) / "l.jsonl"
    d.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return read(str(d))


if __name__ == "__main__":
    unittest.main()
