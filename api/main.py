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

from . import projections as proj
from .settings import Settings
from .store import LogStore

# How often the event stream looks for a longer log. The log grows at most once per engine
# verdict, so a second is already finer than the data.
POLL_S = 1.0


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = LogStore(settings.log_path, settings.pipeline)

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
    app.state.store = store

    def cfg_json() -> dict[str, Any]:
        c = settings.pipeline
        return {
            "log_path": settings.log_path,
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
        return cfg_json()

    RUN = Query(
        None,
        description="Which process run to read; the newest by default, -1 for the whole file.",
    )

    @app.get("/api/revision", summary="Cheap change check")
    def get_revision() -> dict[str, Any]:
        """Poll this. The revision changes only when the log grows, so the UI can skip a refetch."""
        return store.snapshot().meta()

    @app.get("/api/runs", summary="The process runs this log file holds")
    def get_runs() -> dict[str, Any]:
        """A log is appended across restarts. Durations are only valid inside one run."""
        snap = store.snapshot()
        return {"revision": snap.revision, "current": snap.run_index, "runs": snap.runs}

    @app.get("/api/health", summary="What the log contains, and what is missing")
    def get_health(run: int | None = RUN) -> dict[str, Any]:
        snap = store.snapshot(run)
        return snap.meta() | proj.health(snap)

    @app.get("/api/games", summary="One row per game")
    def get_games(run: int | None = RUN) -> dict[str, Any]:
        snap = store.snapshot(run)
        return {"revision": snap.revision, "games": proj.games(snap, settings.pipeline)}

    @app.get("/api/games/{game_id}", summary="Plies, evaluations, state changes, signals")
    def get_game(game_id: str, run: int | None = RUN) -> dict[str, Any]:
        snap = store.snapshot(run)
        body = proj.game(snap, game_id, settings.pipeline)
        if body is None:
            raise HTTPException(status_code=404, detail=f"no game {game_id!r} in the log")
        return {"revision": snap.revision} | body

    @app.get("/api/market", summary="The two-sided tape, per leg")
    def get_market(run: int | None = RUN) -> dict[str, Any]:
        snap = store.snapshot(run)
        return {"revision": snap.revision} | proj.market(snap)

    @app.get("/api/signals", summary="Triggers, confidence, and hypothetical positions")
    def get_signals(run: int | None = RUN) -> dict[str, Any]:
        snap = store.snapshot(run)
        return {"revision": snap.revision} | proj.signals(snap)

    @app.get("/api/recent", summary="The raw tail of the log")
    def get_recent(limit: int = Query(100, ge=1, le=2000), run: int | None = RUN) -> dict[str, Any]:
        snap = store.snapshot(run)
        return {"revision": snap.revision, "records": proj.recent(snap, limit)}

    @app.get("/api/dashboard", summary="Everything the overview needs, in one call")
    def get_dashboard(
        recent_limit: int = Query(60, ge=0, le=500), run: int | None = RUN
    ) -> dict[str, Any]:
        """One request per refresh. Separate endpoints exist, but polling four of them means four
        chances to render a half-updated page."""
        snap = store.snapshot(run)
        return (
            snap.meta()
            | {
                "config": cfg_json(),
                "health": proj.health(snap),
                "games": proj.games(snap, settings.pipeline),
                "market": proj.market(snap),
                "signals": proj.signals(snap),
                "recent": proj.recent(snap, recent_limit) if recent_limit else [],
            }
        )

    @app.get("/api/events", summary="Server-sent revision ticks")
    async def get_events() -> StreamingResponse:
        """Emits one `message` per change, carrying the meta block. The payload is deliberately
        small: the client decides what to refetch."""

        async def gen() -> AsyncIterator[bytes]:
            last = -1
            while True:
                meta = store.snapshot().meta()
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
