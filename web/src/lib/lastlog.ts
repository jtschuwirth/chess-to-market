/**
 * The log the operator last looked at.
 *
 * Without this, every visit to the overview lands on whichever file the server happens to consider
 * newest, and coming back from a game drops you into a different event than the one you left.
 *
 * Per browser, never read back by the API, and fine to lose — so `localStorage`, wrapped, because
 * it throws in a private window.
 */
const KEY = "chess-to-market:log";

export const remember = (log: string | null): void => {
  try {
    log ? localStorage.setItem(KEY, log) : localStorage.removeItem(KEY);
  } catch {
    /* private window, blocked storage: the server default is a fine fallback */
  }
};

export const recall = (): string | null => {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
};

/** Keep a link pointing at the log being viewed, so "Overview" returns to the same event. */
export const linkWithLog = (id: string, path: string, log: string | null): void => {
  const el = document.getElementById(id) as HTMLAnchorElement | null;
  if (el) el.href = log ? `${path}?log=${encodeURIComponent(log)}` : path;
};
