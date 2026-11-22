"""One parse of the log, shared by every request.

The log is append-only, so a cache keyed on `(mtime_ns, size)` is enough: if neither changed, the
parse and the pipeline run are still valid. `revision` increments whenever they do, which is what
the dashboard polls instead of re-downloading a payload that has not changed.

Re-running the whole pipeline on a change is deliberate. The pipeline is pure, and the alternative
— incremental state — is the kind of thing that disagrees with a backtest over the same log, which
would make the dashboard untrustworthy exactly when it matters.

## Runs are a fact of the data, not a view of it

One file holds every run ever made, and `mono_ms` restarts with each process. That matters for
three things, all of them internal: the trigger tracker must not reason across a boundary, a
processing latency must not be measured across one, and the ordering check must not report a
restart as corruption.

It does **not** mean showing one run at a time. Everything the measurement produces already runs on
the wall clock — triggers, tape alignment, entry latency, P&L — and the wall clock is continuous
across a restart. So the whole file is served, on a wall-clock axis, and a run boundary is simply
somewhere the analysis declines to join two records together.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any

from analysis.config_defaults import PipelineConfig
from analysis.observations import ParseReport, Record, check_ordering, read, split_runs
from analysis.pipeline import Result, run

__all__ = ["LogStore", "Snapshot", "split_runs"]


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
    runs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def t0_mono(self) -> int:
        return self.records[0].mono_ms if self.records else 0

    @property
    def t0_wall(self) -> float:
        return self.records[0].ts if self.records else 0.0

    def rel(self, record: Record) -> float:
        """Seconds since the first record, by the **wall** clock.

        Wall rather than monotonic because this is a display axis shared by every source, and
        `mono_ms` restarts with each process — which put signals hours away from the evaluations
        that produced them the first time a log spanned a restart. Within a run the two agree;
        across one, only this is continuous.
        """
        return round(record.ts - self.t0_wall, 3)

    def meta(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "records": len(self.records),
            "lines": self.report.total,
            "missing": self.missing,
            "mtime": self.mtime,
            "size": self.size,
            "t0_wall": self.t0_wall,
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
        self._snap: Snapshot | None = None

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
                    "first_index": a,
                    "last_index": b - 1,
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
            self._records, self._bounds, self._snap = [], [], None
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
        self._snap = None

    def snapshot(self) -> Snapshot:
        """The whole file, parsed at most once per change."""
        with self._lock:
            self._reload()
            if self._missing:
                return Snapshot(revision=self._revision, missing=True, runs=[])
            if self._snap is not None and self._snap.revision == self._revision:
                return self._snap
            self._snap = Snapshot(
                revision=self._revision,
                records=self._records,
                report=self._report,
                result=run(self._records, self.cfg),
                ordering=check_ordering(self._records),
                mtime=self._mtime,
                size=self._size,
                missing=False,
                runs=self._runs_meta(),
            )
            return self._snap
