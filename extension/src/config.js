/**
 * Shared settings and small helpers for every extension surface.
 *
 * Loaded as an ES module by the service worker, the popup and the options page —
 * the only three places that touch `chrome.storage`, so the defaults and the
 * shapes stored under them live here rather than being repeated per page.
 */

/** Settings shown on the options page; also the fallbacks when nothing is stored. */
export const DEFAULTS = {
  // Where `scorekeeper-api` listens. Anything but the default needs the matching
  // host permission — see requestApiPermission().
  apiUrl: "http://localhost:8001",
  // Default metric-selection use case sent with every capture.
  useCase: "default",
};

/** Key under which the run history is cached, in `chrome.storage.local`. */
export const RUNS_KEY = "runs";

/** Legacy single-run key, read once to migrate into {@link RUNS_KEY}. */
const LEGACY_RUN_KEY = "lastRun";

/** How many runs to keep; the oldest beyond this are dropped on insert. */
const RUNS_CAP = 25;

/** Run statuses that will never change again, so polling can stop. */
export const TERMINAL_STATUSES = ["completado", "parcial", "fallido"];

/** Spanish labels for the API's run statuses, shown across every surface. */
export const STATUS_LABELS = {
  ingerido: "ingerido",
  en_cola: "en cola",
  en_proceso: "en proceso",
  completado: "completado",
  parcial: "parcial",
  fallido: "fallido",
};

/**
 * Read the stored settings, filling in DEFAULTS for anything unset.
 *
 * Settings live in `chrome.storage.local`, not `sync`: the API URL is usually a
 * localhost port that only means something on this machine, and the host
 * permission that goes with it is granted per profile anyway, so syncing it to
 * another browser would carry over a URL that extension cannot reach.
 */
export async function getSettings() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...stored, apiUrl: trimSlash(stored.apiUrl || DEFAULTS.apiUrl) };
}

/** Merge `patch` into the stored settings. */
export async function setSettings(patch) {
  await chrome.storage.local.set(patch);
}

/**
 * The submitted-run history, newest first (empty when nothing has been sent).
 *
 * Migrates a pre-history `lastRun` on first read so an in-flight run survives the
 * upgrade, then drops the legacy key so the migration runs only once.
 */
export async function getRuns() {
  const stored = await chrome.storage.local.get([RUNS_KEY, LEGACY_RUN_KEY]);
  if (Array.isArray(stored[RUNS_KEY])) return stored[RUNS_KEY];

  const legacy = stored[LEGACY_RUN_KEY];
  const runs = legacy ? [legacy] : [];
  await chrome.storage.local.set({ [RUNS_KEY]: runs });
  await chrome.storage.local.remove(LEGACY_RUN_KEY);
  return runs;
}

/** One run from the history by `runId`, or `null`. */
export async function getRun(runId) {
  return (await getRuns()).find((run) => run.runId === runId) ?? null;
}

/**
 * Insert `run` (newest first) or replace the existing entry with the same
 * `runId`, cap the history, persist, and return the new list.
 */
export async function upsertRun(run) {
  const rest = (await getRuns()).filter((entry) => entry.runId !== run.runId);
  const runs = [run, ...rest].slice(0, RUNS_CAP);
  await chrome.storage.local.set({ [RUNS_KEY]: runs });
  return runs;
}

/** Drop a trailing slash so `${apiUrl}/api/v1/captures` never doubles up. */
export function trimSlash(url) {
  return String(url).trim().replace(/\/+$/, "");
}

/**
 * A chat URL reduced to `origin + pathname` for matching a run to its tab.
 *
 * The conversation lives in the path (e.g. `claude.ai/chat/<id>`); the `?query`
 * and `#hash` are volatile (tracking params, scroll anchors) and a new message
 * never changes them, so both are dropped. `null` when the URL is unparseable.
 */
export function normalizeChatUrl(url) {
  try {
    const parsed = new URL(url);
    return `${parsed.origin}${parsed.pathname}`;
  } catch {
    return null;
  }
}

/**
 * The `origin/*` match pattern for an API URL, or `null` when it is unparseable.
 * Chrome grants host access per origin, not per path.
 */
export function originPattern(apiUrl) {
  try {
    return `${new URL(trimSlash(apiUrl)).origin}/*`;
  } catch {
    return null;
  }
}

/**
 * Ensure the extension may call `apiUrl`.
 *
 * Only `http://localhost:8001` is granted at install time; pointing the extension
 * at any other host needs a runtime grant, which Chrome only allows from a user
 * gesture (hence: called from the options page's button, never the worker).
 */
export async function requestApiPermission(apiUrl) {
  const pattern = originPattern(apiUrl);
  if (!pattern) return false;
  if (await chrome.permissions.contains({ origins: [pattern] })) return true;
  return chrome.permissions.request({ origins: [pattern] });
}

/**
 * Turn free text into a filename-safe slug (accents folded, lowercase, dashed).
 * Used to derive a scenario id from the page title.
 */
export function slugify(text, maxLength = 48) {
  return String(text)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "") // strip the accents NFD just split off
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, maxLength)
    .replace(/-+$/, "");
}

/** A sortable `YYYY-MM-DD-HH-MM` stamp in local time, for default scenario ids. */
export function timestamp(date = new Date()) {
  const pad = (value) => String(value).padStart(2, "0");
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate()),
    pad(date.getHours()),
    pad(date.getMinutes()),
  ].join("-");
}
