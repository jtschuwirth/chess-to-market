"""The HTTP surface. Every route is a GET; the server has no way to change anything.

    cd code && python -m api --log logs/run.jsonl

The Astro dashboard talks to this, and so can curl. Interactive docs at `/docs`.

Read-only by construction, which is the same contract the Rust side keeps: this reads one local
file and serves what the analysis makes of it. It holds no keys and places no orders.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from pathlib import Path

from . import logs
from . import projections as proj
from .settings import Settings
from .store import LogStore

# How often the event stream looks for a longer log. The log grows at most once per engine
# verdict, so a second is already finer than the data.
POLL_S = 1.0


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    root = Path(settings.log_dir)
    pinned = Path(settings.log_path) if settings.log_path else None

    # One store per file, kept so switching between rounds does not re-parse what is already read.
    stores: dict[str, LogStore] = {}

    def catalogue() -> list[logs.LogFile]:
        return logs.discover(root, pinned)

    def pick(rel: str | None) -> tuple[str, LogStore]:
        """The log a request is about: the one asked for, else the pinned one, else the newest."""
        files = catalogue()
        allowed = {f.path for f in files}
        chosen: str | None = None
        if rel:
            if logs.resolve(root, rel, allowed) is None:
                raise HTTPException(status_code=404, detail=f"no log {rel!r} under {root}")
            chosen = rel
        elif pinned is not None:
            chosen = next((f.path for f in files if Path(f.path).name == pinned.name), None)
            chosen = chosen or pinned.as_posix()
        elif files:
            chosen = files[0].path
        if chosen is None:
            # Nothing on disk yet. Report against the expected path so the UI can say so.
            chosen = (pinned or root / "run.jsonl").as_posix()
        path = logs.resolve(root, chosen, allowed) or Path(chosen)
        store = stores.get(chosen)
        if store is None:
            store = LogStore(str(path), settings.pipeline)
            stores[chosen] = store
        return chosen, store

    app = FastAPI(
        title="chess-to-market analysis API",
        description=(
            "Reads the observation log the Rust tool writes and serves what `analysis` makes of "
            "it. Read-only: no endpoint places an order, holds a key or touches funds. Every "
            "P&L figure is hypothetical, computed from recorded prices."
        ),
        version="0.1.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.stores = stores

    def cfg_json(log_path: str | None = None) -> dict[str, Any]:
        c = settings.pipeline
        return {
            "log_dir": settings.log_dir,
            "log_path": log_path or settings.log_path,
            "eval_depth": c.eval_depth,
            "latency_s": c.latency_s,
            "white_leg": c.white_leg,
            "black_leg": c.black_leg,
            "resolved_leg": c.resolved_leg,
            "trigger": asdict(c.trigger) | {"kinds": [k.value for k in c.trigger.kinds]},
            "paper": asdict(c.paper),
        }

    @app.get("/api/config", summary="How this server was started")
    def get_config() -> dict[str, Any]:
        """Thresholds, latency and leg mapping in force. Set at launch; no endpoint changes them."""
        return cfg_json(pick(None)[0])

    LOG = Query(
        None,
        description="Which log to read, as `<tournament>/<round>.jsonl`; the newest by default.",
    )

    @app.get("/api/logs", summary="The logs on disk, grouped by event")
    def get_logs() -> dict[str, Any]:
        """One entry per tournament, each with its rounds, newest first."""
        files = catalogue()
        return {
            "root": settings.log_dir,
            "current": pick(None)[0],
            "events": logs.grouped(files),
            "count": len(files),
        }


    @app.get("/api/revision", summary="Cheap change check")
    def get_revision(log: str | None = LOG) -> dict[str, Any]:
        """Poll this. The revision changes only when the log grows, so the UI can skip a refetch."""
        return pick(log)[1].snapshot().meta()

    @app.get("/api/runs", summary="The process runs this log file holds")
    def get_runs(log: str | None = LOG) -> dict[str, Any]:
        """Informational. The whole file is always served; a run boundary is only somewhere the
        analysis declines to join two records — a tracker is not carried over one, and a duration
        is never measured across one."""
        chosen, store = pick(log)
        snap = store.snapshot()
        return {"log": chosen, "revision": snap.revision, "runs": snap.runs}

    @app.get("/api/health", summary="What the log contains, and what is missing")
    def get_health(log: str | None = LOG) -> dict[str, Any]:
        chosen, store = pick(log)
        snap = store.snapshot()
        return {"log": chosen} | snap.meta() | proj.health(snap)

    @app.get("/api/games", summary="One row per game")
    def get_games(log: str | None = LOG) -> dict[str, Any]:
        chosen, store = pick(log)
        snap = store.snapshot()
        return {"log": chosen, "revision": snap.revision, "games": proj.games(snap, settings.pipeline)}

    @app.get("/api/games/{game_id}", summary="Plies, evaluations, state changes, signals")
    def get_game(game_id: str, log: str | None = LOG) -> dict[str, Any]:
        chosen, store = pick(log)
        snap = store.snapshot()
        body = proj.game(snap, game_id, settings.pipeline)
        if body is None:
            raise HTTPException(status_code=404, detail=f"no game {game_id!r} in the log")
        return {"log": chosen, "revision": snap.revision} | body

    @app.get("/api/market", summary="The two-sided tape, per leg")
    def get_market(log: str | None = LOG) -> dict[str, Any]:
        chosen, store = pick(log)
        snap = store.snapshot()
        return {"log": chosen, "revision": snap.revision} | proj.market(snap)

    @app.get("/api/signals", summary="Triggers, confidence, and hypothetical positions")
    def get_signals(log: str | None = LOG) -> dict[str, Any]:
        chosen, store = pick(log)
        snap = store.snapshot()
        return {"log": chosen, "revision": snap.revision} | proj.signals(snap)

    @app.get("/api/recent", summary="The raw tail of the log")
    def get_recent(limit: int = Query(100, ge=1, le=2000), log: str | None = LOG) -> dict[str, Any]:
        chosen, store = pick(log)
        snap = store.snapshot()
        return {"log": chosen, "revision": snap.revision, "records": proj.recent(snap, limit)}

    @app.get("/api/dashboard", summary="Everything the overview needs, in one call")
    def get_dashboard(
        recent_limit: int = Query(60, ge=0, le=500),
        log: str | None = LOG,
    ) -> dict[str, Any]:
        """One request per refresh. Separate endpoints exist, but polling four of them means four
        chances to render a half-updated page."""
        chosen, store = pick(log)
        snap = store.snapshot()
        return (
            {"log": chosen}
            | snap.meta()
            | {
                "config": cfg_json(chosen),
                "logs": logs.grouped(catalogue()),
                "health": proj.health(snap),
                "games": proj.games(snap, settings.pipeline),
                "market": proj.market(snap),
                "signals": proj.signals(snap),
                "recent": proj.recent(snap, recent_limit) if recent_limit else [],
            }
        )

    @app.get("/api/events", summary="Server-sent revision ticks")
    async def get_events(log: str | None = LOG) -> StreamingResponse:
        """Emits one `message` per change, carrying the meta block. The payload is deliberately
        small: the client decides what to refetch."""

        chosen, store = pick(log)

        async def gen() -> AsyncIterator[bytes]:
            last = -1
            while True:
                meta = {"log": chosen} | store.snapshot().meta()
                if meta["revision"] != last:
                    last = meta["revision"]
                    yield f"data: {json.dumps(meta)}\n\n".encode()
                else:
                    # A comment keeps the connection warm through proxies without faking an event.
                    yield b": keepalive\n\n"
                await asyncio.sleep(POLL_S)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()
