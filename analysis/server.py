"""Live dashboard for debugging a running bot. Standard library only — no dependencies.

    cd code && python -m analysis serve logs/run.jsonl

Serves a single page on http://127.0.0.1:8787 that re-reads the log every couple of seconds. Built
for one job: **seeing what the Rust is and is not emitting**, while it runs.

Binds to 127.0.0.1 only. It serves one local file read-only and accepts no input.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config_defaults import PipelineConfig
from .observations import check_ordering, read
from .pipeline import run

UI = Path(__file__).parent / "ui" / "index.html"


def state(log_path: str, cfg: PipelineConfig) -> dict:
    """Everything the page needs, in one JSON payload."""
    try:
        records, rep = read(log_path)
    except FileNotFoundError:
        return {"error": f"log not found: {log_path}", "records": 0}

    res = run(records, cfg)
    h = res.health
    t0 = records[0].mono_ms if records else 0

    series: dict[str, list] = {"white_win": [], "draw": [], "black_win": []}
    for r in records:
        if r.source != "eval":
            continue
        if cfg.eval_depth is not None and r.payload.get("depth") != cfg.eval_depth:
            continue
        wdl = r.wdl_white()
        if wdl is None:
            continue
        x = (r.mono_ms - t0) / 1000.0
        series["white_win"].append([x, round(wdl[0], 4)])
        series["draw"].append([x, round(wdl[1], 4)])
        series["black_win"].append([x, round(wdl[2], 4)])

    legs: dict[str, list] = {}
    for t in res.tape:
        legs.setdefault(t["leg"], []).append([
            round(t["t"] - records[0].ts, 2) if records else 0,
            t.get("ask"), t.get("bid"),
        ])

    signals = [{
        "ply": s.ply, "san": s.san, "kind": s.trigger.kind.value,
        "before": s.trigger.before.value, "after": s.trigger.after.value,
        "x": round(s.ts - records[0].ts, 2) if records else 0,
        "room": None if s.room is None else round(s.room, 3),
        "findability": round(s.findability, 3),
        "confidence": round(s.confidence, 3),
        "note": s.findability_note,
        "position": None if s.position is None else {
            "leg": s.position.leg,
            "entry": round(s.position.entry_price, 4),
            "exit": round(s.position.exit_price, 4),
            "reason": s.position.exit_reason,
            "pnl": round(s.position.pnl_usd, 2),
            "fees": round(s.position.fees_usd, 4),
        },
    } for s in res.signals]

    return {
        "log": log_path,
        "records": rep.kept,
        "total_lines": rep.total,
        "span_s": round(h.span_s, 1),
        "counts": h.counts,
        "health": h.report().split("\n"),
        "problems": rep.problems[:30],
        "ordering": check_ordering(records)[:10],
        "depths": sorted(h.depths_seen),
        "legs": sorted(h.legs_seen),
        "series": series,
        "leg_series": legs,
        "signals": signals,
        "pnl": round(res.pnl_usd, 2),
        "fees": round(res.fees_usd, 4),
        "notes": h.notes[-25:],
        "recent": [{
            "x": round((r.mono_ms - t0) / 1000.0, 2),
            "source": r.source,
            "text": _describe(r),
        } for r in records[-60:]],
    }


def _describe(r) -> str:
    p = r.payload
    if r.source == "ply":
        return f"ply {p.get('ply')} {p.get('san')}"
    if r.source == "eval":
        w = r.wdl_white()
        wdl = f"W{w[0]:.2f} D{w[1]:.2f} L{w[2]:.2f}" if w else "no wdl_white"
        return f"d{p.get('depth')} ply {p.get('ply')} {wdl} ({len(r.lines())} lines)"
    if r.source == "market":
        return f"{p.get('leg')} {p.get('event_type')} bid={p.get('best_bid')} ask={p.get('best_ask')}"
    if r.source == "signal":
        return f"{p.get('kind')} conf={p.get('confidence')} {p.get('reason')}"
    return str(p.get("message", ""))


def serve(log_path: str, port: int = 8787, cfg: PipelineConfig | None = None) -> None:
    cfg = cfg or PipelineConfig()
    html = UI.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/state"):
                body = json.dumps(state(log_path, cfg)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
            elif self.path in ("/", "/index.html"):
                body = html
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # keep the console clear for the bot's own output
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://127.0.0.1:{port}   watching {log_path}")
    print("ctrl-c to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
