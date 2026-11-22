# api — HTTP over the analysis

FastAPI server that reads the observation log the Rust writes and serves what `analysis` makes of
it. Every number comes from `analysis`, unchanged.

`analysis` stays standard-library only: a backtest needs no dependencies and no server. This package
is the only thing that needs installing.

```powershell
cd code; python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r api\requirements.txt
cd code; .\.venv\Scripts\python.exe -m api
```

It serves every log under `--log-dir` (default `logs`), opening the most recently written one. The
dashboard switches between them with its event and round pickers — no restart. `--log <file>` pins
one instead.

Interactive docs at <http://127.0.0.1:8000/docs>.

Flags mirror `python -m analysis`, so a dashboard and a `replay` pointed at the same log with the
same flags are guaranteed to agree: `--depth`, `--latency`, `--hold`, `--notional`, `--fee-rate`,
`--white`, `--black`, `--resolved`, plus `--host`, `--port`, `--reload`, `--origin`.

## Read-only, by construction

Every route is a `GET`. The server reads one local file, binds to localhost by default, holds no
keys and places no orders. Settings are fixed at launch — no endpoint changes them. Every P&L figure
is hypothetical, computed from recorded prices, and the payload says so (`"hypothetical": true`).

## Endpoints

| Route | What it gives |
|---|---|
| `/api/dashboard` | everything the overview needs, at one revision |
| `/api/revision` | cheap change check — poll this |
| `/api/events` | server-sent revision ticks |
| `/api/health` | counts, diagnoses, schema and ordering problems |
| `/api/games` · `/api/games/{id}` | per-game rows · plies, evaluations, state changes, signals |
| `/api/market` | the two-sided tape per leg |
| `/api/signals` | triggers, confidence, hypothetical positions |
| `/api/recent` | the raw tail |
| `/api/runs` · `/api/config` | the process runs in the file · the settings in force |
| `/api/logs` | every log on disk, grouped by event |

Most routes take `?log=<tournament>/<round>.jsonl` and `?run=N` (`-1` for the whole file). The
newest log and its newest run are the defaults, and every response says which log it answered from.

`?log=` is the one place a request chooses a path, so only paths `/api/logs` already lists are
accepted — which rules out traversal without having to reason about `..`.

## Game headers

Game rows and the game detail carry `title`, `white`, `black`, ratings, `result`, `winner`,
`finished`, `board`, `event` and `url`, taken from the latest `game` record for that id.

`finished` is `null`, not `false`, when the log carries no `game` record: "nobody told us" and
"still in play" are different states, and showing the second for the first would be the dashboard
inventing a fact. The PGN result token is the only honest end-of-game signal — decisive games
between titled players end in resignations, so the final position is not mate and the board says
nothing. `url` is the `GameURL` header verbatim, never constructed.

## Runs, and why they exist

The tool appends to its log, so one file usually holds several process runs — and `mono_ms` restarts
with each process. Subtracting across a boundary gives a duration wrong by however long the tool was
stopped, and a `StateTracker` carried across one would compare the end of yesterday's game with the
start of today's.

So the file is split into runs and everything works on one at a time. A boundary is where the
monotonic clock goes backwards, or where the two clocks disagree about how much time passed: inside
one process they advance together, including through a stall, so drift between them means a
different process — or a system clock that stepped, which is equally a reason not to subtract
across it.

This is not hypothetical. The first real log had one stale note from an earlier run at the top; with
the whole file treated as one span, signals timed off the wall clock landed **5 hours 45 minutes**
away from evaluations timed off the monotonic one, on the same chart.

## Layout

| file | what |
|---|---|
| `settings.py` | what the server was started with |
| `store.py` | one parse per change, run splitting, the revision counter |
| `projections.py` | snapshot → JSON shapes; no analysis happens here |
| `main.py` | the routes |
| `runtime.py` | ASGI entry point (`uvicorn api.runtime:app`) |

```powershell
cd code; .\.venv\Scripts\python.exe -m unittest discover -s api -t .
```
