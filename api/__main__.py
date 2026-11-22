"""Launch the API.

    cd code
    python -m api --log logs/run.jsonl
    python -m api --log logs/run.jsonl --white gukesh --black sindarov --fee-rate 0.05

Flags mirror `python -m analysis` so a dashboard and a replay can be pointed at the same log with
the same settings and are then guaranteed to agree.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace

from analysis.config_defaults import PipelineConfig
from analysis.paper import PaperConfig

from .settings import ENV_VAR, Settings


def build(argv: list[str] | None = None) -> tuple[Settings, str, int, bool]:
    ap = argparse.ArgumentParser(prog="python -m api", description=__doc__)
    ap.add_argument("--log", default="logs/run.jsonl", help="JSONL observation log to read")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default localhost only)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="reload on code changes")
    ap.add_argument("--depth", type=int, default=16, help="eval depth to decide on; 0 for all")
    ap.add_argument("--latency", type=float, default=10.0, help="modelled entry latency, seconds")
    ap.add_argument("--hold", type=float, default=120.0, help="timed exit, seconds")
    ap.add_argument("--notional", type=float, default=10.0, help="USD per hypothetical position")
    ap.add_argument("--fee-rate", type=float, default=0.0, dest="fee_rate", help="taker fee rate")
    ap.add_argument("--white", default=None, help="market leg that is white in this game")
    ap.add_argument("--black", default=None, help="market leg that is black")
    ap.add_argument("--resolved", default=None, help="leg that won, for settled positions")
    ap.add_argument(
        "--origin",
        action="append",
        default=None,
        help="extra CORS origin for the dashboard (repeatable)",
    )
    args = ap.parse_args(argv)

    cfg = PipelineConfig(
        eval_depth=None if args.depth == 0 else args.depth,
        latency_s=args.latency,
        white_leg=args.white,
        black_leg=args.black,
        resolved_leg=args.resolved,
    )
    cfg = replace(
        cfg,
        paper=PaperConfig(
            notional_usd=args.notional, max_hold_s=args.hold, fee_rate=args.fee_rate
        ),
    )
    defaults = Settings()
    settings = Settings(
        log_path=args.log,
        pipeline=cfg,
        cors_origins=defaults.cors_origins + tuple(args.origin or ()),
    )
    return settings, args.host, args.port, args.reload


def main(argv: list[str] | None = None) -> int:
    try:
        import uvicorn
    except ModuleNotFoundError:
        print(
            "uvicorn is not installed. The analysis package needs nothing; the API does:\n"
            "    python -m pip install -r api/requirements.txt",
            file=sys.stderr,
        )
        return 1

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    settings, host, port, reload = build(raw_argv)

    # Set before importing the app: importing `runtime` *builds* it, and it reads this. Importing
    # first and setting after is how `--log` ended up silently ignored.
    os.environ[ENV_VAR] = json.dumps(raw_argv)
    print(f"API:  http://{host}:{port}/api/dashboard   docs: http://{host}:{port}/docs")
    print(f"log:  {settings.log_path}")
    uvicorn.run("api.runtime:app", host=host, port=port, reload=reload, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
