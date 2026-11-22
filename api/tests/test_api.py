"""API tests. Needs `pip install -r api/requirements.txt`; the analysis tests still need nothing.

What is worth testing here is not FastAPI but the two things that would make the dashboard lie:
the cache must not serve a stale log, and the API's numbers must equal the pipeline's.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from analysis.config_defaults import PipelineConfig
from analysis.observations import Record, read
from analysis.pipeline import run
from analysis.synth import generate
from api import logs  # needs no FastAPI
from api.store import split_runs

try:
    from fastapi.testclient import TestClient

    from api.main import create_app
    from api.settings import Settings
except ModuleNotFoundError:  # pragma: no cover - the analysis suite runs without FastAPI
    TestClient = None


def write_log(lines: list[str]) -> Path:
    p = Path(tempfile.mkdtemp()) / "run.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.log = write_log(generate())
        self.cfg = PipelineConfig(white_leg="white", black_leg="black")
        self.client = TestClient(create_app(Settings(log_path=str(self.log), pipeline=self.cfg)))

    def test_config_reports_what_the_server_was_given(self) -> None:
        body = self.client.get("/api/config").json()
        self.assertEqual(body["white_leg"], "white")
        # Paths are reported posix-style, so a log path survives a URL and a Windows/Linux swap.
        self.assertEqual(body["log_path"], self.log.as_posix())
        self.assertEqual(body["eval_depth"], 16)

    def test_health_carries_the_pipeline_diagnoses(self) -> None:
        body = self.client.get("/api/health").json()
        records, _ = read(str(self.log))
        self.assertEqual(body["report"], run(records, self.cfg).health.report().split("\n"))
        self.assertFalse(body["log_missing"])

    def test_games_rows_match_the_log(self) -> None:
        games = self.client.get("/api/games").json()["games"]
        self.assertTrue(games)
        g = games[0]
        self.assertEqual(g["plies"], len([r for r in read(str(self.log))[0] if r.source == "ply"]))
        self.assertIn(g["state"], {"white_winning", "black_winning", "level", None})

    def test_signals_equal_the_pipeline(self) -> None:
        body = self.client.get("/api/signals").json()
        records, _ = read(str(self.log))
        res = run(records, self.cfg)
        self.assertEqual(len(body["signals"]), len(res.signals))
        self.assertAlmostEqual(body["pnl_usd"], round(res.pnl_usd, 4))
        self.assertTrue(body["hypothetical"], "P&L must never be presented as realised")

    def test_game_detail_and_404(self) -> None:
        gid = self.client.get("/api/games").json()["games"][0]["game_id"]
        body = self.client.get(f"/api/games/{gid}").json()
        self.assertEqual(body["game_id"], gid)
        self.assertTrue(body["plies"])
        self.assertEqual(self.client.get("/api/games/not-a-game").status_code, 404)

    def test_every_eval_point_has_both_clocks(self) -> None:
        # Mixing the two axes is the mistake that silently ruins a latency figure, so the shape
        # has to carry both rather than leave the client to guess which one it got.
        gid = self.client.get("/api/games").json()["games"][0]["game_id"]
        for p in self.client.get(f"/api/games/{gid}").json()["evals"]:
            self.assertIn("t", p)
            self.assertIn("ts", p)
            self.assertGreater(p["ts"], 1_000_000_000, "ts is unix seconds")

    def test_dashboard_is_one_consistent_revision(self) -> None:
        body = self.client.get("/api/dashboard").json()
        self.assertEqual(body["revision"], self.client.get("/api/revision").json()["revision"])
        for key in ("config", "health", "games", "market", "signals", "recent"):
            self.assertIn(key, body)

    def test_a_missing_log_is_reported_not_raised(self) -> None:
        client = TestClient(create_app(Settings(log_path="logs/does-not-exist.jsonl")))
        body = client.get("/api/health").json()
        self.assertTrue(body["log_missing"])
        self.assertEqual(client.get("/api/games").json()["games"], [])


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestCaching(unittest.TestCase):
    def test_appending_to_the_log_bumps_the_revision(self) -> None:
        records = generate()
        log = write_log(records[:40])
        client = TestClient(create_app(Settings(log_path=str(log))))

        first = client.get("/api/revision").json()
        self.assertEqual(client.get("/api/revision").json()["revision"], first["revision"],
                         "an unchanged log must not re-parse")

        with log.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(records[40:]) + "\n")

        second = client.get("/api/revision").json()
        self.assertGreater(second["revision"], first["revision"])
        self.assertGreater(second["records"], first["records"])


if __name__ == "__main__":
    unittest.main()


class TestRunSplitting(unittest.TestCase):
    """A log file is appended across restarts, and `mono_ms` restarts with the process."""

    def rec(self, mono: int, wall: int) -> Record:
        return Record(mono, wall, "note", {"source": "note", "message": "x"})

    def test_a_single_run_is_one_slice(self) -> None:
        rs = [self.rec(i * 1000, 1_000_000_000_000 + i * 1000) for i in range(5)]
        self.assertEqual(split_runs(rs), [(0, 5)])

    def test_a_monotonic_reset_starts_a_run(self) -> None:
        rs = [self.rec(5000, 1_000_000_005_000), self.rec(10, 1_000_000_010_000)]
        self.assertEqual(split_runs(rs), [(0, 1), (1, 2)])

    def test_clocks_drifting_apart_starts_a_run(self) -> None:
        # The giveaway for an appended restart: mono advanced 2s while the wall advanced 6 hours.
        rs = [self.rec(1000, 1_000_000_001_000), self.rec(3000, 1_000_021_600_000)]
        self.assertEqual(split_runs(rs), [(0, 1), (1, 2)])

    def test_a_long_stall_is_not_a_new_run(self) -> None:
        # Both clocks advance together through a stall, so it must stay one run.
        rs = [self.rec(1000, 1_000_000_001_000), self.rec(601_000, 1_000_000_601_000)]
        self.assertEqual(split_runs(rs), [(0, 2)])

    def test_an_empty_log_has_no_runs(self) -> None:
        self.assertEqual(split_runs([]), [])


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestRunSelection(unittest.TestCase):
    def setUp(self) -> None:
        old = generate()[:3]
        new = generate()
        shifted = []
        for line in new:
            o = json.loads(line)
            o["wall_unix_ms"] += 6 * 3600 * 1000  # the restart, six hours later
            shifted.append(json.dumps(o))
        self.log = write_log(old + shifted)
        self.client = TestClient(create_app(Settings(log_path=str(self.log))))

    def test_the_newest_run_is_served_by_default(self) -> None:
        runs = self.client.get("/api/runs").json()
        self.assertEqual(len(runs["runs"]), 2)
        self.assertEqual(runs["current"], 1)
        self.assertEqual(self.client.get("/api/revision").json()["records"], runs["runs"][1]["records"])

    def test_an_earlier_run_can_be_asked_for(self) -> None:
        meta = self.client.get("/api/health?run=0").json()
        self.assertEqual(meta["run_index"], 0)
        self.assertEqual(meta["records"], 3)

    def test_run_minus_one_is_the_whole_file(self) -> None:
        meta = self.client.get("/api/health?run=-1").json()
        self.assertEqual(meta["records"], meta["lines"])

    def test_signals_and_evals_share_one_axis_inside_a_run(self) -> None:
        # The bug this guards: a signal timed off the wall clock and an evaluation timed off the
        # monotonic one land hours apart on the chart once a log spans a restart.
        gid = self.client.get("/api/games").json()["games"][0]["game_id"]
        body = self.client.get(f"/api/games/{gid}").json()
        if not body["signals"] or not body["evals"]:
            self.skipTest("the reference log produced no signal for this game")
        span = max(e["t"] for e in body["evals"])
        for s in body["signals"]:
            self.assertLessEqual(s["t"], span + 1.0)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestGameHeaders(unittest.TestCase):
    """Names, result and the broadcast link — and what happens when the log has none of them."""

    def client_for(self, lines: list[str]) -> "TestClient":
        return TestClient(create_app(Settings(log_path=str(write_log(lines)))))

    def test_names_become_the_title_and_the_result_marks_it_finished(self) -> None:
        g = self.client_for(generate()).get("/api/games").json()["games"][0]
        self.assertEqual(g["title"], "Ding, Liren — Gukesh, D")
        self.assertEqual(g["white_elo"], 2728)
        self.assertEqual(g["result"], "0-1")
        self.assertTrue(g["finished"])
        self.assertEqual(g["winner"], "black")
        self.assertTrue(g["url"].startswith("https://lichess.org/broadcast/"))

    def test_the_latest_game_record_wins(self) -> None:
        # The game is seen in play first and finished later; the dashboard must show the finish.
        lines = generate()
        self.assertEqual(
            sum(1 for ln in lines if json.loads(ln)["payload"]["source"] == "game"),
            2,
            "the reference log must exercise both sightings",
        )
        detail = self.client_for(lines).get("/api/games/synth-g14").json()
        self.assertEqual(detail["result"], "0-1")

    def test_a_log_without_game_records_falls_back_to_the_id(self) -> None:
        lines = [ln for ln in generate() if json.loads(ln)["payload"]["source"] != "game"]
        g = self.client_for(lines).get("/api/games").json()["games"][0]
        self.assertEqual(g["title"], g["game_id"])
        self.assertIsNone(g["url"])
        self.assertIsNone(
            g["finished"], "unknown must not be shown as in-play — that would invent a fact"
        )

    def test_a_game_in_play_is_not_finished(self) -> None:
        lines = [
            ln
            for ln in generate()
            if json.loads(ln)["payload"].get("result") != "0-1"
        ]
        g = self.client_for(lines).get("/api/games").json()["games"][0]
        self.assertFalse(g["finished"])
        self.assertIsNone(g["winner"])

    def test_a_game_record_alone_is_enough_to_list_a_game(self) -> None:
        # A board that has not moved yet still belongs on the dashboard.
        lines = [ln for ln in generate() if json.loads(ln)["payload"]["source"] in ("note", "game")]
        games = self.client_for(lines).get("/api/games").json()["games"]
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["plies"], 0)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestLaunchFlags(unittest.TestCase):
    def test_the_flags_reach_the_app(self) -> None:
        # The regression: `runtime` builds the app at import time from this variable, so a launcher
        # that imports it before setting the variable serves the default log and says nothing.
        import importlib
        import os

        from api.settings import ENV_VAR

        before = os.environ.get(ENV_VAR)
        os.environ[ENV_VAR] = json.dumps(["--log", "logs/elsewhere.jsonl", "--depth", "12"])
        try:
            import api.runtime

            mod = importlib.reload(api.runtime)
            self.assertEqual(mod.app.state.settings.log_path, "logs/elsewhere.jsonl")
            self.assertEqual(mod.app.state.settings.pipeline.eval_depth, 12)
        finally:
            if before is None:
                os.environ.pop(ENV_VAR, None)
            else:
                os.environ[ENV_VAR] = before


class TestLogDiscovery(unittest.TestCase):
    """The picker's data: what is on disk, grouped by event."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.event = self.root / "world-junior-2026-open"
        self.event.mkdir()

    def write(self, path: Path, lines: list[str]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_rounds_group_under_their_event(self) -> None:
        self.write(self.event / "round-5.jsonl", generate())
        self.write(self.event / "round-6.jsonl", generate())
        events = logs.grouped(logs.discover(self.root))
        self.assertEqual(len(events), 1)
        self.assertEqual(
            sorted(r["round_slug"] for r in events[0]["rounds"]), ["round-5", "round-6"]
        )

    def test_the_event_name_comes_from_the_log_not_the_slug(self) -> None:
        # A slug is unreadable in a dropdown; the relay's own name is not.
        self.write(self.event / "round-5.jsonl", generate())
        events = logs.grouped(logs.discover(self.root))
        self.assertEqual(events[0]["name"], "Synthetic Championship 2024")

    def test_a_log_with_no_game_record_falls_back_to_its_slug(self) -> None:
        self.write(self.event / "round-9.jsonl", ['{"mono_ms":0,"wall_unix_ms":1,"payload":{"source":"note","message":"x"}}'])
        f = [x for x in logs.discover(self.root) if x.round_slug == "round-9"][0]
        self.assertEqual(f.event_name, "World Junior 2026 Open")
        self.assertEqual(f.round_name, "Round 9")

    def test_the_round_name_can_come_from_the_opening_note(self) -> None:
        note = (
            '{"mono_ms":0,"wall_unix_ms":1,"payload":{"source":"note","message":'
            '"lichess: FIDE World Junior · Round 5 (yRwFuCyK), startsAt 1795295700000"}}'
        )
        self.write(self.event / "r.jsonl", [note])
        f = [x for x in logs.discover(self.root) if x.round_slug == "r"][0]
        self.assertEqual(f.event_name, "FIDE World Junior")
        self.assertEqual(f.round_name, "Round 5")

    def test_loose_logs_at_the_root_are_kept_separate(self) -> None:
        self.write(self.root / "demo.jsonl", generate())
        self.write(self.event / "round-5.jsonl", generate())
        slugs = {e["slug"] for e in logs.grouped(logs.discover(self.root))}
        self.assertIn(logs.LOOSE, slugs)
        self.assertIn("world-junior-2026-open", slugs)

    def test_newest_first(self) -> None:
        import os, time

        a = self.write(self.event / "old.jsonl", generate())
        b = self.write(self.event / "new.jsonl", generate())
        os.utime(a, (time.time() - 9999, time.time() - 9999))
        self.assertEqual(logs.discover(self.root)[0].path, "world-junior-2026-open/new.jsonl")

    def test_a_malformed_log_still_appears(self) -> None:
        # A half-written line costs a label, never the whole listing.
        self.write(self.event / "broken.jsonl", ["{not json", '{"mono_ms":']);
        self.assertIn("world-junior-2026-open/broken.jsonl", {f.path for f in logs.discover(self.root)})

    def test_resolve_refuses_anything_not_listed(self) -> None:
        # The one place a request chooses a path. Only listed paths are accepted, so traversal
        # never has to be reasoned about.
        self.write(self.event / "round-5.jsonl", generate())
        allowed = {f.path for f in logs.discover(self.root)}
        self.assertIsNotNone(logs.resolve(self.root, "world-junior-2026-open/round-5.jsonl", allowed))
        for bad in ("../../etc/passwd", "world-junior-2026-open/../../x.jsonl", "/etc/passwd", ""):
            self.assertIsNone(logs.resolve(self.root, bad, allowed), bad)


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestLogSelection(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "ev").mkdir()
        for name in ("round-1.jsonl", "round-2.jsonl"):
            (self.root / "ev" / name).write_text("\n".join(generate()) + "\n", encoding="utf-8")
        self.client = TestClient(create_app(Settings(log_dir=str(self.root))))

    def test_logs_lists_the_events_and_rounds(self) -> None:
        body = self.client.get("/api/logs").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["events"]), 1)
        self.assertIn(body["current"], {"ev/round-1.jsonl", "ev/round-2.jsonl"})

    def test_a_request_can_choose_its_log(self) -> None:
        body = self.client.get("/api/games?log=ev/round-1.jsonl").json()
        self.assertEqual(body["log"], "ev/round-1.jsonl")
        self.assertTrue(body["games"])

    def test_an_unknown_log_is_a_404(self) -> None:
        self.assertEqual(self.client.get("/api/games?log=ev/nope.jsonl").status_code, 404)
        self.assertEqual(self.client.get("/api/games?log=../../etc/passwd").status_code, 404)

    def test_an_empty_directory_still_answers(self) -> None:
        client = TestClient(create_app(Settings(log_dir=str(Path(tempfile.mkdtemp())))))
        self.assertEqual(client.get("/api/logs").json()["count"], 0)
        self.assertTrue(client.get("/api/health").json()["log_missing"])
