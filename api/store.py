"""One parse of the log, shared by every request.

The log is append-only, so a cache keyed on `(mtime_ns, size)` is enough: if neither changed, the
parse and the pipeline run are still valid. `revision` increments whenever they do, which is what
the dashboard polls instead of re-downloading a payload that has not changed.

Re-running the whole pipeline on a change is deliberate. The pipeline is pure, and the alternative
— incremental state — is the kind of thing that disagrees with a backtest over the same log, which
would make the dashboard untrustworthy exactly when it matters.

## Runs

One log file usually holds several process runs, because the tool appends. That matters more than
it sounds: `mono_ms` restarts at zero with each process, so subtracting across a boundary gives a
duration that is wrong by however long the tool was stopped, and a `StateTracker` carried across one
would compare the end of yesterday's game with the start of today's.

So the log is split into runs and everything downstream works on one run at a time — the newest by
default. A boundary is where the monotonic clock goes backwards, or where the two clocks disagree
about how much time passed: within a process they advance together, including through a stall, so
drift between them means a different process (or a system clock that stepped, which is equally a
reason not to subtract across it).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any

from analysis.config_defaults import PipelineConfig
from analysis.observations import ParseReport, Record, check_ordering, read
from analysis.pipeline import Result, run

# How far the two clocks may disagree before the records belong to different runs.
DRIFT_TOLERANCE_MS = 30_000


def split_runs(records: list[Record]) -> list[tuple[int, int]]:
    """`[start, end)` index pairs, one per process run, in log order."""
    if not records:
        return []
    bounds: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(records)):
        prev, cur = records[i - 1], records[i]
        d_mono = cur.mono_ms - prev.mono_ms
        d_wall = cur.wall_unix_ms - prev.wall_unix_ms
        if d_mono < 0 or abs(d_wall - d_mono) > DRIFT_TOLERANCE_MS:
            bounds.append((start, i))
            start = i
    bounds.append((start, len(records)))
    return bounds


@dataclass
class Snapshot:
    """A consistent view: one run's records, its pipeline result, one revision number."""

    revision: int = 0
    records: list[Record] = field(default_factory=list)
    report: ParseReport = field(default_factory=ParseReport)
    result: Result = field(default_factory=Result)
    ordering: list[str] = field(default_factory=list)
    mtime: float = 0.0
    size: int = 0
    missing: bool = True
    run_index: int = 0
    runs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def t0_mono(self) -> int:
        return self.records[0].mono_ms if self.records else 0

    @property
    def t0_wall(self) -> float:
        return self.records[0].ts if self.records else 0.0

    def rel(self, record: Record) -> float:
        """Seconds since this run started, by the monotonic clock."""
        return round((record.mono_ms - self.t0_mono) / 1000.0, 3)

    def meta(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "records": len(self.records),
            "lines": self.report.total,
            "missing": self.missing,
            "mtime": self.mtime,
            "size": self.size,
            "t0_wall": self.t0_wall,
            "run_index": self.run_index,
            "run_count": len(self.runs),
            "runs": self.runs,
        }


class LogStore:
    """Parses the log on demand, at most once per change, and serves one run at a time."""

    def __init__(self, log_path: str, cfg: PipelineConfig) -> None:
        self.log_path = log_path
        self.cfg = cfg
        self._lock = threading.Lock()
        self._revision = 0
        self._stat: tuple[int, int] | None = None
        self._records: list[Record] = []
        self._report = ParseReport()
        self._bounds: list[tuple[int, int]] = []
        self._mtime = 0.0
        self._size = 0
        self._missing = True
        self._cache: dict[int, Snapshot] = {}

    def _runs_meta(self) -> list[dict[str, Any]]:
        out = []
        for i, (a, b) in enumerate(self._bounds):
            chunk = self._records[a:b]
            out.append(
                {
                    "index": i,
                    "records": len(chunk),
                    "started_unix": chunk[0].ts if chunk else 0.0,
                    "span_s": round((chunk[-1].mono_ms - chunk[0].mono_ms) / 1000.0, 1)
                    if chunk
                    else 0.0,
                }
            )
        return out

    def _reload(self) -> None:
        try:
            st = os.stat(self.log_path)
        except FileNotFoundError:
            if not self._missing or not self._revision:
                self._revision += 1
            self._stat = None
            self._records, self._bounds, self._cache = [], [], {}
            self._report = ParseReport()
            self._missing = True
            return

        key = (st.st_mtime_ns, st.st_size)
        if key == self._stat and not self._missing:
            return

        self._records, self._report = read(self.log_path)
        self._bounds = split_runs(self._records)
        self._mtime, self._size = st.st_mtime, st.st_size
        self._missing = False
        self._stat = key
        self._revision += 1
        self._cache = {}

    def snapshot(self, run_index: int | None = None) -> Snapshot:
        """The newest run by default; `run_index` selects an earlier one, `-1` means all records."""
        with self._lock:
            self._reload()
            if self._missing or not self._bounds:
                return Snapshot(revision=self._revision, missing=self._missing, runs=[])

            idx = len(self._bounds) - 1 if run_index is None else run_index
            if idx == -1:
                a, b = 0, len(self._records)
            else:
                idx = max(0, min(idx, len(self._bounds) - 1))
                a, b = self._bounds[idx]

            cached = self._cache.get(idx)
            if cached is not None:
                return cached

            chunk = self._records[a:b]
            snap = Snapshot(
                revision=self._revision,
                records=chunk,
                report=self._report,
                result=run(chunk, self.cfg),
                ordering=check_ordering(chunk),
                mtime=self._mtime,
                size=self._size,
                missing=False,
                run_index=idx,
                runs=self._runs_meta(),
            )
            self._cache[idx] = snap
            return snap
