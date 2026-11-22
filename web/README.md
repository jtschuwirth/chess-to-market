# web — the dashboard

Astro front end over the FastAPI analysis server. It exists to **watch the bot run and debug the
Rust**: which producers are emitting, what the engine thinks, where the market is, and which signals
fired.

It is read-only. It can place no order, because nothing it talks to can.

## Running it

Two processes. The API first:

```powershell
cd code; .\.venv\Scripts\python.exe -m api
```

Then the dashboard:

```powershell
cd code\web; npm run dev
```

Open <http://127.0.0.1:4321>. In development the dev server proxies `/api` to
`http://127.0.0.1:8000`, so the browser sees one origin and there is no CORS and no base URL in the
client. Point it elsewhere with `API_ORIGIN`.

For a built site served from somewhere else, set `PUBLIC_API_BASE` to the API's absolute URL at
build time; the API already allows the dev origins and takes `--origin` for others.

## Pages

| Page | What it answers |
|---|---|
| `/` | Is every producer alive? Which games exist, who is winning, which are priced, what fired? |
| `/game?id=<game_id>` | For one game: engine probabilities against market price over time, that game's market tape, our own processing latency, signals, state changes, and every move with its reconstructed time. |

Which event you are looking at is carried in `?log=` and remembered in `localStorage`, so the
Overview button returns to the event you came from and a fresh visit opens the one you looked at
last rather than whichever file the server considers newest. A remembered log that has since been
deleted falls back to the server's choice instead of erroring.

Both poll `/api/events` (server-sent) and fall back to polling `/api/revision`. The revision changes
only when the log grows, so an idle run costs one small request per tick.

## Two things worth knowing before changing a chart

**Engine probability and market price share one y-axis on purpose.** They are the same unit — the
gap between them is the quantity this project exists to measure. Engine lines are solid, market
lines dashed; colour follows the leg, not the source. A second y-axis would make the gap meaningless.

**Event, round and run are three different selectors.** The Rust writes one file per round under a
folder per tournament, so event and round pick a *file*. A file can still hold several process runs
if you restarted the same round, which is the third selector — it appears only when a file has more
than one.

**The x-axis is seconds since the current *run* started.** A log file is appended across restarts,
so `mono_ms` restarts inside it. The API splits the file into runs and serves the newest; the
selector appears only when a file holds more than one. See `api/store.py`.

## Layout

```
web/
├── astro.config.mjs      dev proxy to the API
└── src/
    ├── layouts/Shell.astro   page shell, theme toggle, connection badge
    ├── lib/api.ts            typed client; `watch()` for live updates
    ├── lib/chart.ts          inline-SVG line chart: crosshair, tooltip, signal rules
    ├── lib/format.ts         formatting helpers
    ├── pages/index.astro     overview
    ├── pages/game.astro      one game
    └── styles/global.css     tokens, light and dark
```

No UI framework and no chart library: the whole dashboard is one dependency (`astro`). The chart is
~250 lines of SVG because what it needs — a crosshair, dashed overlays and vertical signal rules on
a shared axis — is most of what a chart library would be configured away from anyway.

The palette is the data-viz default (blue / orange / aqua), validated for colour-vision deficiency
in both light and dark. Series are also direct-labelled and every chart has a table view, so
identity is never carried by colour alone.
