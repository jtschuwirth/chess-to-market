export const pct = (v: number | null | undefined): string =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;

export const price = (v: number | null | undefined): string =>
  v === null || v === undefined ? "—" : v.toFixed(3);

export const usd = (v: number | null | undefined): string =>
  v === null || v === undefined ? "—" : `${v < 0 ? "-" : "+"}$${Math.abs(v).toFixed(2)}`;

export const num = (v: number | null | undefined): string =>
  v === null || v === undefined ? "—" : String(v);

export const secs = (v: number): string => {
  if (!isFinite(v)) return "—";
  if (Math.abs(v) < 90) return `${v.toFixed(v < 10 ? 1 : 0)}s`;
  const m = Math.floor(v / 60);
  if (m < 90) return `${m}m ${Math.round(v - m * 60)}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
};

export const clock = (unixMs: number | null | undefined): string =>
  unixMs ? new Date(unixMs).toISOString().slice(11, 19) : "—";

export const stateLabel = (s: string | null): string =>
  s === "white_winning"
    ? "White winning"
    : s === "black_winning"
      ? "Black winning"
      : s === "level"
        ? "Level"
        : "No evaluation";

/** Move number and side, from a 1-based half-move index. */
export const moveOf = (ply: number | null): string =>
  ply === null ? "—" : `${Math.ceil(ply / 2)}${ply % 2 === 1 ? "." : "..."}`;

export const el = (html: string): HTMLElement => {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild as HTMLElement;
};

export const esc = (s: unknown): string =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
