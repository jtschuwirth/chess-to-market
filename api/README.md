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

## What it writes, and what it does not

Every route is a `GET` except `/api/markets`, and all those write is **one local file naming public
market-data feeds to subscribe to**. No key is held, nothing is signed, and no order is placed — the
tool being steered has no ability to place one. Settings are still fixed at launch; no endpoint
changes them. Every P&L figure is hypothetical, computed from recorded prices, and the payload says
so (`"hypothetical": true`).

A pasted Polymarket link is **never fetched**. A slug is extracted from it and used against a fixed
host (`gamma-api.polymarket.com`), so a URL from a browser cannot make this server reach an
arbitrary address. A link to any other host is refused by name.

## Endpoints

| Route | What it gives |
|---|---|
| `/api/dashboard` | everything the overview needs, at one revision |
| `/api/revision` | cheap change check — poll this |
| `/api/events` | server-sent revision ticks |
| `/api/health` | counts, diagnoses, schema and ordering problems |
| `/api/games` · `/api/games/{id}` | per-game rows · plies, evaluations, state changes, signals |
| `/api/market` | one game's two-sided tape (`?game_id=`), or every priced game |
| `/api/signals` | triggers, confidence, hypothetical positions |
| `/api/recent` | the raw tail |
| `/api/runs` · `/api/config` | the process runs in the file · the settings in force |
| `/api/logs` | every log on disk, grouped by event |
| `/api/markets` | which markets the running tool is told to watch |
| `/api/markets/resolve?url=` | a Polymarket link to its outcomes and asset ids |
| `PUT /api/markets/{game_id}` | watch, or stop watching, a game's market |
| `DELETE /api/markets/{game_id}` | forget it entirely |

Most routes take `?log=<tournament>/<round>.jsonl`; the newest is the default, and every response
says which log it answered from. There is no run parameter — the whole file is always served.

`?log=` is the one place a request chooses a path, so only paths `/api/logs` already lists are
accepted — which rules out traversal without having to reason about `..`.

## Adding a market while the tool runs

`PUT /api/markets/{game_id}` writes `<log stem>.markets.json` beside the round's log. The running
Rust re-reads that file about once a second and subscribes or unsubscribes **on its open socket** —
it does not restart and does not reconnect.

The file is desired state, not commands, which is what makes it safe to write from a UI: a missed
read converges on the next one, a restart picks up a market chosen while the tool was down, and
"stop watching" is a field rather than a message that has to arrive. `enabled: false` keeps the
entry so the dashboard can offer it again without re-resolving the link. The contract is
`src/control.rs`; `api/markets.py` mirrors it, and `test_what_is_written_is_what_the_rust_reads`
pins the shape.

## Markets are per game

A market belongs to a **game**, not to a round: a tournament relays thirty boards and each priced
game has its own market. The tape is keyed by `game_id` throughout, `/api/games/{id}` carries that
game's market, and a game row says `has_market` so the overview can mark the priced boards.

Legs labelled `white`, `draw` and `black` need no further configuration — the engine's `wdl_white`
is already in those terms. Player-name legs still need `--white` / `--black`, except `draw`, which
means the same thing whoever is playing and lines up on its own.

## Game headers

Game rows and the game detail carry `title`, `white`, `black`, ratings, `result`, `winner`,
`finished`, `board`, `event` and `url`, taken from the latest `game` record for that id.

`finished` is `null`, not `false`, when the log carries no `game` record: "nobody told us" and
"still in play" are different states, and showing the second for the first would be the dashboard
inventing a fact. The PGN result token is the only honest end-of-game signal — decisive games
between titled players end in resignations, so the final position is not mate and the board says
nothing. `url` is the `GameURL` header verbatim, never constructed.

## Processing latency

`/api/games/{id}` carries a `latency` block: per ply, how long from logging the move to its
evaluation being available, what the engine's own search cost at each depth, and the difference —
time the position spent *not* being searched.

**Measured on `mono_ms`, and only within a run.** It is a duration, which is what the monotonic
clock is for, and a pair either side of a restart is hours apart on the wall clock while being
nothing of the sort.

It excludes Lichess's relay delay, which is theirs and invisible from in here. `depths_emitted_together`
counts the moves whose depths all reached the log at the same instant — when that is most of them,
the cheap early depth is buying no earliness.

## Runs, and why they exist

The tool appends to its log, so one file usually holds several process runs — and `mono_ms` restarts
with each process. Subtracting across a boundary gives a duration wrong by however long the tool was
stopped, and a `StateTracker` carried across one would compare the end of yesterday's game with the
start of today's.

So the file is split into runs — but that is an internal seam, not a view. Everything the
measurement produces already runs on the wall clock (triggers, tape alignment, entry latency, P&L),
and the wall clock is continuous across a restart. The whole file is served, on a wall-clock axis,
and a boundary is simply somewhere the analysis declines to join two records: a trigger tracker is
not carried over one, a processing latency is not measured across one, and the ordering check does
not report one as corruption.

A boundary is where the monotonic clock goes backwards **while real time did not**, or where the two
clocks disagree about how much time passed. Inside one process they advance together, including
through a forty-minute think — which is ordinary classical chess and precisely the event this
project exists to catch, so it must never split.

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
