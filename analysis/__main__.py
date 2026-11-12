"""CLI for the analysis side.

    cd code
    python -m analysis synth    --out logs/demo.jsonl      # reference log matching SCHEMA.md
    python -m analysis validate logs/run.jsonl             # does the Rust emit a valid log?
    python -m analysis replay   logs/run.jsonl             # signals, positions, summary
    python -m analysis serve    logs/run.jsonl             # live dashboard on :8787

Read-only throughout. Nothing here places an order.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from .config_defaults import PipelineConfig
from .observations import check_ordering, read
from .paper import PaperConfig
from .pipeline import run


def _cfg(args) -> PipelineConfig:
    cfg = PipelineConfig(
        eval_depth=args.depth,
        latency_s=args.latency,
        white_leg=args.white,
        black_leg=args.black,
        resolved_leg=args.resolved,
    )
    return replace(cfg, paper=PaperConfig(
        notional_usd=args.notional,
        max_hold_s=args.hold,
        fee_rate=args.fee_rate,
    ))


def cmd_synth(args) -> int:
    from .synth import write
    n = write(args.out)
    print(f"wrote {n} records to {args.out}")
    print("point the Rust at the same shape and diff the two logs — see SCHEMA.md")
    return 0


def cmd_validate(args) -> int:
    records, rep = read(args.log)
    print(rep.summary())
    order = check_ordering(records)
    if order:
        print(f"\nordering problems ({len(order)}):")
        for p in order[:20]:
            print(f"  {p}")
    res = run(records, _cfg(args))
    print("\n" + res.health.report())
    bad = bool(rep.problems or order)
    print("\n" + ("FAIL — see above" if bad else "OK — log satisfies the contract"))
    return 1 if bad else 0


def cmd_replay(args) -> int:
    records, rep = read(args.log)
    if rep.problems:
        print(f"note: {len(rep.problems)} schema problem(s); run `validate` for detail\n")
    res = run(records, _cfg(args))
    print(res.health.report())
    print()
    if not res.signals:
        print("no signals. A quiet game produces none, which is correct behaviour.")
        return 0
    print(f"{'t':>8s} {'ply':>4s} {'move':<10s} {'transition':<28s} "
          f"{'room':>5s} {'find':>5s} {'conf':>5s}  position")
    t0 = records[0].ts
    for s in res.signals:
        pos = "—"
        if s.position:
            p = s.position
            pos = (f"{p.leg} {p.entry_price:.3f}->{p.exit_price:.3f} "
                   f"{p.exit_reason} {p.pnl_usd:+.2f} (fee {p.fees_usd:.2f})")
        room = f"{s.room:.2f}" if s.room is not None else "  — "
        print(f"{s.ts - t0:8.1f} {str(s.ply):>4s} {(s.san or '—'):<10s} "
              f"{str(s.trigger):<28s} {room:>5s} {s.findability:5.3f} {s.confidence:5.3f}  {pos}")
    if res.positions:
        print(f"\n{len(res.positions)} positions · P&L ${res.pnl_usd:+.2f} · fees ${res.fees_usd:.2f}")
        print("Hypothetical. No orders were placed.")
    return 0


def cmd_serve(args) -> int:
    from .server import serve
    serve(args.log, args.port, _cfg(args))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m analysis", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("log", help="path to the JSONL observation log")
        p.add_argument("--depth", type=int, default=16,
                       help="eval depth to decide on; 0 for all (default 16)")
        p.add_argument("--latency", type=float, default=10.0,
                       help="modelled entry latency in seconds (default 10)")
        p.add_argument("--hold", type=float, default=120.0,
                       help="timed exit in seconds (default 120)")
        p.add_argument("--notional", type=float, default=10.0, help="USD per position")
        p.add_argument("--fee-rate", type=float, default=0.0, dest="fee_rate",
                       help="taker fee rate; 0 matches the pre-2026 tape (default 0)")
        p.add_argument("--white", default=None, help="market leg that is white in this game")
        p.add_argument("--black", default=None, help="market leg that is black")
        p.add_argument("--resolved", default=None,
                       help="leg that won, for positions that reach settlement")

    p = sub.add_parser("synth", help="write a reference log")
    p.add_argument("--out", default="logs/demo.jsonl")
    p.set_defaults(fn=cmd_synth)

    p = sub.add_parser("validate", help="check a log against SCHEMA.md")
    common(p)
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("replay", help="run the pipeline over a log")
    common(p)
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("serve", help="live dashboard")
    common(p)
    p.add_argument("--port", type=int, default=8787)
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    if getattr(args, "depth", None) == 0:
        args.depth = None
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
