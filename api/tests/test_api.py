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
from api import markets as mk
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
class TestRunsAreInternal(unittest.TestCase):
    """A restart is a seam in the data, not a view to choose between."""

    def setUp(self) -> None:
        old = generate()[:3]
        new = []
        for line in generate():
            o = json.loads(line)
            o["wall_unix_ms"] += 6 * 3600 * 1000  # the restart, six hours later
            new.append(json.dumps(o))
        self.log = write_log(old + new)
        self.client = TestClient(create_app(Settings(log_path=str(self.log))))

    def test_the_whole_file_is_served(self) -> None:
        meta = self.client.get("/api/revision").json()
        self.assertEqual(meta["records"], meta["lines"])
        self.assertEqual(meta["run_count"], 2)

    def test_runs_are_reported_but_not_selected(self) -> None:
        body = self.client.get("/api/runs").json()
        self.assertEqual(len(body["runs"]), 2)
        self.assertNotIn("current", body, "nothing picks a run any more")

    def test_a_restart_is_not_an_ordering_fault(self) -> None:
        self.assertEqual(self.client.get("/api/health").json()["ordering_problems"], [])

    def test_signals_and_evals_share_one_axis_across_a_restart(self) -> None:
        # The bug this guards: an evaluation timed off the monotonic clock and a signal timed off
        # the wall clock land hours apart once a log spans a restart.
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


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestProcessingLatency(unittest.TestCase):
    """How long we took to react to a move — the one number a code change can move."""

    BASE = 1_764_000_000_000

    def rec(self, mono: int, payload: dict) -> str:
        return json.dumps(
            {"mono_ms": mono, "wall_unix_ms": self.BASE + mono, "payload": payload}
        )

    def ply(self, mono: int, ply: int) -> str:
        return self.rec(
            mono,
            {"source": "ply", "game_id": "g", "ply": ply, "san": "e4", "uci": "e2e4",
             "fen": "x", "move_unix_ms": self.BASE + mono},
        )

    def eval_(self, mono: int, ply: int, depth: int, search_ms: int) -> str:
        return self.rec(
            mono,
            {"source": "eval", "game_id": "g", "ply": ply, "depth": depth,
             "search_ms": search_ms, "wdl_white": [100, 800, 100]},
        )

    def client_for(self, lines: list[str]) -> "TestClient":
        return TestClient(create_app(Settings(log_path=str(write_log(lines)))))

    def latency(self, lines: list[str]) -> dict:
        return self.client_for(lines).get("/api/games/g").json()["latency"]

    def test_total_is_from_the_move_to_its_evaluation(self) -> None:
        lat = self.latency([self.ply(1000, 1), self.eval_(2600, 1, 16, 1400)])
        row = lat["per_ply"][0]
        self.assertEqual(row["total_ms"], 1600)
        self.assertEqual(row["searches"], {"16": 1400})
        self.assertEqual(row["queue_ms"], 200, "total minus the deepest search")

    def test_one_row_per_ply_carrying_every_depth(self) -> None:
        lat = self.latency(
            [self.ply(1000, 1), self.eval_(1100, 1, 12, 70), self.eval_(2600, 1, 16, 1400)]
        )
        self.assertEqual(len(lat["per_ply"]), 1, "a ply is one move, however many depths it got")
        row = lat["per_ply"][0]
        self.assertEqual(row["searches"], {"12": 70, "16": 1400})
        self.assertEqual(row["first_eval_ms"], 100, "depth 12 arrived early here")
        self.assertEqual(row["total_ms"], 1600)

    def test_depths_arriving_together_are_counted(self) -> None:
        # The engine batches its depths until the search ends, so the cheap early depth buys no
        # earliness. Worth seeing rather than inferring from two identical columns.
        lat = self.latency(
            [self.ply(1000, 1), self.eval_(2600, 1, 12, 70), self.eval_(2600, 1, 16, 1400)]
        )
        self.assertEqual(lat["depths_emitted_together"], 1)
        self.assertEqual(lat["per_ply"][0]["first_eval_ms"], lat["per_ply"][0]["total_ms"])

    def test_the_summary_is_what_a_tweak_has_to_move(self) -> None:
        lines = [self.ply(0, 1), self.eval_(100, 1, 16, 90),
                 self.ply(1000, 2), self.eval_(1300, 2, 16, 280),
                 self.ply(2000, 3), self.eval_(2900, 3, 16, 880)]
        s = self.latency(lines)["summary"]
        self.assertEqual(s["n"], 3)
        self.assertEqual(s["median_ms"], 300)
        self.assertEqual(s["max_ms"], 900)
        self.assertEqual(s["searches"], [{"depth": 16, "median_ms": 280, "n": 3}])

    def test_a_latency_is_never_measured_across_a_restart(self) -> None:
        # The move was logged before the restart, the evaluation after it. On the wall clock those
        # are six hours apart; as a processing latency the pair is meaningless, not slow.
        restart = json.dumps(
            {"mono_ms": 20, "wall_unix_ms": self.BASE + 6 * 3600_000,
             "payload": {"source": "eval", "game_id": "g", "ply": 1, "depth": 16,
                         "search_ms": 10, "wdl_white": [100, 800, 100]}}
        )
        lat = self.latency([self.ply(1000, 1), restart])
        self.assertEqual(lat["per_ply"], [], "the pair spans a boundary, so there is no latency")

    def test_an_evaluation_with_no_matching_move_is_skipped(self) -> None:
        lat = self.latency([self.eval_(2600, 7, 16, 1400)])
        self.assertEqual(lat["per_ply"], [])

    def test_no_engine_means_no_latency_rather_than_zero(self) -> None:
        lat = self.latency([self.ply(1000, 1), self.ply(2000, 2)])
        self.assertEqual(lat["summary"]["n"], 0)
        self.assertEqual(lat["summary"]["searches"], [])


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestMarketPerGame(unittest.TestCase):
    """A market belongs to a game. The overview says which boards have one; the game view has it."""

    def setUp(self) -> None:
        self.client = TestClient(create_app(Settings(log_path=str(write_log(generate())))))

    def test_the_game_carries_its_own_market(self) -> None:
        body = self.client.get("/api/games/synth-g14").json()
        self.assertEqual(body["market"]["game_id"], "synth-g14")
        self.assertTrue(body["market"]["legs"])
        self.assertTrue(body["market"]["last"])

    def test_a_game_row_says_whether_it_is_priced(self) -> None:
        rows = self.client.get("/api/games").json()["games"]
        self.assertTrue(rows[0]["has_market"])

    def test_asking_for_another_game_gives_an_empty_tape(self) -> None:
        body = self.client.get("/api/market?game_id=not-a-game").json()
        self.assertEqual(body["legs"], [])
        self.assertEqual(body["games"], ["synth-g14"], "but it still says which games are priced")

    def test_omitting_the_game_returns_everything_priced(self) -> None:
        body = self.client.get("/api/market").json()
        self.assertIsNone(body["game_id"])
        self.assertTrue(body["legs"])


class TestMarketLinks(unittest.TestCase):
    """Turning what the operator pasted into asset ids, without fetching what they pasted."""

    def test_a_link_yields_the_market_then_the_event(self) -> None:
        # Most specific first: the last segment is usually the market, the one before it the event.
        self.assertEqual(
            mk.slugs_from("https://polymarket.com/event/wcc-2026/will-gukesh-win-game-1"),
            ["will-gukesh-win-game-1", "wcc-2026"],
        )

    def test_a_bare_slug_works(self) -> None:
        self.assertEqual(mk.slugs_from("will-gukesh-win-game-1"), ["will-gukesh-win-game-1"])

    def test_another_host_is_refused(self) -> None:
        # The slug is sent to a fixed host, but a link to somewhere else is a mistake worth naming
        # rather than quietly searching Polymarket for its path.
        with self.assertRaises(mk.ResolveError):
            mk.slugs_from("https://evil.example.com/event/x")

    def test_nothing_slug_shaped_is_refused(self) -> None:
        for bad in ("", "   ", "!!!", "https://polymarket.com/"):
            with self.assertRaises(mk.ResolveError):
                mk.slugs_from(bad)

    def test_outcomes_are_double_encoded_json(self) -> None:
        # `outcomes` and `clobTokenIds` are JSON strings inside the JSON. Parse them twice.
        out = mk._outcomes({"outcomes": '["Yes", "No"]', "clobTokenIds": '["111", "222"]'})
        self.assertEqual(out, [{"outcome": "Yes", "asset_id": "111"}, {"outcome": "No", "asset_id": "222"}])

    def test_an_outcome_with_no_token_is_dropped(self) -> None:
        # It cannot be subscribed to, so offering it would be offering a button that does nothing.
        out = mk._outcomes({"outcomes": '["Yes", "No"]', "clobTokenIds": '["111", ""]'})
        self.assertEqual([o["outcome"] for o in out], ["Yes"])

    def test_malformed_outcomes_are_empty_not_fatal(self) -> None:
        self.assertEqual(mk._outcomes({"outcomes": "not json", "clobTokenIds": "[]"}), [])
        self.assertEqual(mk._outcomes({}), [])


class TestControlFile(unittest.TestCase):
    """The file the Rust reads. Both sides must agree on its name and shape."""

    def setUp(self) -> None:
        self.log = Path(tempfile.mkdtemp()) / "round-5.jsonl"

    def test_it_sits_beside_the_log(self) -> None:
        # Mirrors `control::path_for` in the Rust; if these disagree nothing happens at all.
        self.assertEqual(mk.control_path(self.log).name, "round-5.markets.json")
        self.assertEqual(mk.control_path(self.log).parent, self.log.parent)

    def test_a_missing_file_is_no_markets(self) -> None:
        self.assertEqual(mk.read_control(self.log), [])

    def test_upsert_adds_then_replaces(self) -> None:
        legs = [{"leg": "white", "asset_id": "a1"}]
        mk.upsert(self.log, mk.Entry("g1", True, legs, "a-link"))
        mk.upsert(self.log, mk.Entry("g2", True, legs))
        self.assertEqual([m["game_id"] for m in mk.read_control(self.log)], ["g1", "g2"])

        mk.upsert(self.log, mk.Entry("g1", False, legs))
        got = mk.read_control(self.log)
        self.assertEqual([m["game_id"] for m in got], ["g1", "g2"], "order is stable")
        self.assertFalse(got[0]["enabled"])

    def test_disabling_keeps_the_entry(self) -> None:
        # So the UI can offer a market it already resolved without pasting the link again.
        legs = [{"leg": "white", "asset_id": "a1"}]
        mk.upsert(self.log, mk.Entry("g1", False, legs, "a-link"))
        got = mk.read_control(self.log)[0]
        self.assertFalse(got["enabled"])
        self.assertEqual(got["source"], "a-link")

    def test_remove_takes_it_out(self) -> None:
        mk.upsert(self.log, mk.Entry("g1", True, [{"leg": "white", "asset_id": "a1"}]))
        self.assertEqual(mk.remove(self.log, "g1"), [])

    def test_a_corrupt_file_reads_as_empty(self) -> None:
        mk.control_path(self.log).parent.mkdir(parents=True, exist_ok=True)
        mk.control_path(self.log).write_text("{not json", encoding="utf-8")
        self.assertEqual(mk.read_control(self.log), [])

    def test_the_write_is_atomic(self) -> None:
        # The reader is another process polling the file; a rename means it never sees half of one.
        mk.upsert(self.log, mk.Entry("g1", True, [{"leg": "white", "asset_id": "a1"}]))
        self.assertFalse(mk.control_path(self.log).with_suffix(".json.tmp").exists())
        json.loads(mk.control_path(self.log).read_text(encoding="utf-8"))


@unittest.skipIf(TestClient is None, "fastapi not installed")
class TestMarketEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "ev").mkdir()
        self.log = self.root / "ev" / "round-1.jsonl"
        self.log.write_text("\n".join(generate()) + "\n", encoding="utf-8")
        self.client = TestClient(create_app(Settings(log_dir=str(self.root))))

    def test_markets_start_empty_and_name_their_file(self) -> None:
        body = self.client.get("/api/markets").json()
        self.assertEqual(body["markets"], [])
        self.assertTrue(body["control_file"].endswith("ev/round-1.markets.json"))

    def test_a_market_can_be_added_then_disabled_then_removed(self) -> None:
        legs = [{"leg": "white", "asset_id": "a1"}, {"leg": "black", "asset_id": "a2"}]
        r = self.client.put("/api/markets/g1", json={"legs": legs, "source": "a-link"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["markets"][0]["game_id"], "g1")

        r = self.client.put("/api/markets/g1", json={"legs": legs, "enabled": False})
        self.assertFalse(r.json()["markets"][0]["enabled"])

        self.assertEqual(self.client.delete("/api/markets/g1").json()["markets"], [])

    def test_what_is_written_is_what_the_rust_reads(self) -> None:
        # The shape is the contract. A field renamed on either side silently does nothing.
        self.client.put(
            "/api/markets/g1", json={"legs": [{"leg": "white", "asset_id": "a1"}]}
        )
        body = json.loads((self.root / "ev" / "round-1.markets.json").read_text(encoding="utf-8"))
        self.assertEqual(
            body,
            {"markets": [{"game_id": "g1", "enabled": True, "legs": [{"leg": "white", "asset_id": "a1"}]}]},
        )

    def test_a_bad_link_is_a_422_not_a_crash(self) -> None:
        r = self.client.get("/api/markets/resolve", params={"url": "https://evil.example.com/x"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("not a Polymarket link", r.json()["detail"])
