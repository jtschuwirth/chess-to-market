export type Meta = {
  revision: number;
  records: number;
  lines: number;
  missing: boolean;
  mtime: number;
  size: number;
  t0_wall: number;
  run_count: number;
  runs: RunInfo[];
};

export type LogFile = {
  path: string;
  event_slug: string;
  event_name: string;
  round_slug: string;
  round_name: string;
  size: number;
  mtime: number;
};

export type EventGroup = {
  slug: string;
  name: string;
  mtime: number;
  rounds: LogFile[];
};

export type RunInfo = {
  index: number;
  records: number;
  started_unix: number;
  span_s: number;
};

export type Health = {
  log_missing: boolean;
  counts: Record<string, number>;
  span_s: number;
  depths_seen: number[];
  legs_seen: string[];
  evals_with_wdl: number;
  evals_without_wdl: number;
  evals_with_lines: number;
  suppressed_by_cooldown: number;
  report: string[];
  schema_problems: string[];
  schema_problem_count: number;
  ordering_problems: string[];
  unknown_sources: Record<string, number>;
  notes: string[];
};

/** Headers from the PGN. All optional: the log may carry no `game` record yet. */
export type GameInfo = {
  title: string;
  white: string | null;
  black: string | null;
  white_elo: number | null;
  black_elo: number | null;
  white_title: string | null;
  black_title: string | null;
  result: string | null;
  /** `null` means nobody told us, which is not the same as "still in play". */
  finished: boolean | null;
  winner: "white" | "black" | "draw" | null;
  board: string | null;
  event: string | null;
  url: string | null;
};

export type GameRow = GameInfo & {
  game_id: string;
  plies: number;
  evals: number;
  signals: number;
  state: string | null;
  last_ply: number | null;
  last_san: string | null;
  last_fen: string | null;
  last_move_unix_ms: number | null;
  wdl_white: [number, number, number] | null;
  last_seen_t: number;
  has_market: boolean;
  pnl_usd: number;
};

export type PlyPoint = {
  t: number;
  ts: number;
  ply: number;
  san: string | null;
  uci: string | null;
  fen: string | null;
  move_unix_ms: number | null;
};

export type EvalPoint = {
  t: number;
  ts: number;
  ply: number | null;
  depth: number | null;
  search_ms: number | null;
  cp_white: number | null;
  mate_in: number | null;
  only_move_gap_cp: number | null;
  wdl_white: [number, number, number] | null;
  lines: number;
};

export type SignalPoint = {
  game_id: string | null;
  t: number;
  ts: number;
  ply: number | null;
  san: string | null;
  kind: string;
  before: string;
  after: string;
  p_white_win: number;
  p_draw: number;
  p_black_win: number;
  room: number | null;
  findability: number;
  confidence: number;
  note: string;
  implied: Record<string, number>;
  position: {
    leg: string;
    entry_price: number;
    exit_price: number;
    exit_reason: string;
    pnl_usd: number;
    fees_usd: number;
  } | null;
};

export type TapePoint = {
  t: number;
  ts: number;
  bid: number | null;
  ask: number | null;
  mid: number;
};

export type Market = {
  game_id: string | null;
  games: string[];
  legs: string[];
  series: Record<string, TapePoint[]>;
  last: Record<string, TapePoint>;
};

export type Config = {
  log_dir: string;
  log_path: string;
  eval_depth: number | null;
  latency_s: number;
  white_leg: string | null;
  black_leg: string | null;
  resolved_leg: string | null;
  trigger: { enter: number; exit: number; cooldown_s: number; kinds: string[] };
  paper: Record<string, number>;
};

export type FeedRow = {
  t: number;
  ts: number;
  source: string;
  game_id: string | null;
  text: string;
};

export type Dashboard = Meta & {
  log: string;
  logs: EventGroup[];
  config: Config;
  health: Health;
  games: GameRow[];
  market: Market;
  signals: {
    signals: SignalPoint[];
    positions: number;
    pnl_usd: number;
    fees_usd: number;
    hypothetical: boolean;
  };
  recent: FeedRow[];
};

export type LatencyRow = {
  ply: number;
  first_eval_ms: number;
  total_ms: number;
  queue_ms: number;
  searches: Record<string, number | null>;
};

export type Latency = {
  per_ply: LatencyRow[];
  summary: {
    n: number;
    median_ms: number;
    p90_ms: number;
    max_ms: number;
    median_queue_ms: number;
    searches: { depth: number; median_ms: number; n: number }[];
  };
  depths_emitted_together: number;
  measured: string;
};

export type GameDetail = GameInfo & {
  revision: number;
  latency: Latency;
  market: Market;
  game_id: string;
  state: string | null;
  decision_depth: number | null;
  depths: number[];
  plies: PlyPoint[];
  evals: EvalPoint[];
  state_changes: { ply: number | null; ts: number; from: string | null; to: string | null; emitted: boolean }[];
  signals: SignalPoint[];
};

const BASE = import.meta.env.PUBLIC_API_BASE ?? "/api";

export class ApiError extends Error {}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new ApiError(`${res.status} ${res.statusText} on ${path}`);
  return (await res.json()) as T;
}

/** Which log and which run a request is about. Both optional; the server has sensible defaults. */
export type Scope = { log?: string | null };

const qs = (scope: Scope, extra: Record<string, string | number> = {}) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(extra)) p.set(k, String(v));
  if (scope.log) p.set("log", scope.log);
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  logs: () => get<{ root: string; current: string; events: EventGroup[]; count: number }>("/logs"),
  dashboard: (limit = 60, scope: Scope = {}) =>
    get<Dashboard>(`/dashboard${qs(scope, { recent_limit: limit })}`),
  game: (id: string, scope: Scope = {}) =>
    get<GameDetail>(`/games/${encodeURIComponent(id)}${qs(scope)}`),
  market: (scope: Scope = {}, gameId?: string) =>
    get<Market & { revision: number }>(
      `/market${qs(scope, gameId ? { game_id: gameId } : {})}`,
    ),
  revision: (scope: Scope = {}) => get<Meta>(`/revision${qs(scope)}`),
};

/** Calls `onTick` when the log grows. Falls back to polling when SSE is unavailable. */
export function watch(
  onTick: (m: Meta) => void,
  scope: () => Scope = () => ({}),
  intervalMs = 2000,
): () => void {
  let stopped = false;
  let timer = 0;
  let es: EventSource | null = null;

  const poll = () => {
    let last = -1;
    const tick = async () => {
      if (stopped) return;
      try {
        const m = await api.revision(scope());
        if (m.revision !== last) {
          last = m.revision;
          onTick(m);
        }
      } catch {
        /* server down: the next tick retries */
      }
      timer = window.setTimeout(tick, intervalMs);
    };
    tick();
  };

  try {
    const s = scope();
    es = new EventSource(`${BASE}/events${s.log ? `?log=${encodeURIComponent(s.log)}` : ""}`);
    es.onmessage = (ev) => {
      try {
        onTick(JSON.parse(ev.data) as Meta);
      } catch {
        /* ignore a malformed frame */
      }
    };
    es.onerror = () => {
      es?.close();
      es = null;
      if (!stopped && !timer) poll();
    };
  } catch {
    poll();
  }

  return () => {
    stopped = true;
    es?.close();
    if (timer) clearTimeout(timer);
  };
}
