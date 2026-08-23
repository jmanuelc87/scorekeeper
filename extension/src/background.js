/**
 * Service worker: the only place that talks to the Scorekeeper API.
 *
 * The popup does no network work of its own because it is torn down the moment it
 * loses focus, which would abort an in-flight upload. Instead it sends a message
 * here, and the worker injects the content script, POSTs the capture, caches the
 * resulting run and then polls it on an alarm — so progress keeps updating with
 * the popup closed, and reopening it just reads the cached state.
 *
 * Fetches run here for a second reason: a worker request to a host in
 * `host_permissions` is exempt from CORS, so a plain `scorekeeper-api` needs no
 * extra allowed origin. The same request from a content script would not be.
 */

import {
  getRun,
  getRuns,
  getSettings,
  runCaptures,
  TERMINAL_STATUSES,
  upsertRun,
} from "./config.js";

/** Injected on demand; deliberately not a declared content script. */
const CAPTURE_FILE = "src/content/capture.js";

const POLL_ALARM = "scorekeeper-poll";
// Chrome clamps alarms to 30s minimum; scoring takes minutes, so that is plenty.
const POLL_MINUTES = 0.5;

// Once everything has finished successfully, the green ✓ is wiped after this many
// minutes so it does not linger forever; a delay (not an instant clear) keeps the
// "done" cue around long enough to notice. Failures keep their ! instead.
const CLEAR_BADGE_ALARM = "scorekeeper-clear-badge";
const CLEAR_BADGE_MINUTES = 10;

const BADGE = {
  ingerido: { text: "•", color: "#6b7280" },
  en_cola: { text: "…", color: "#8b5cf6" },
  en_proceso: { text: "…", color: "#8b5cf6" },
  completado: { text: "✓", color: "#16a34a" },
  parcial: { text: "!", color: "#d97706" },
  fallido: { text: "!", color: "#dc2626" },
};

const HANDLERS = {
  /** Read the open chat without sending anything, to populate the popup. */
  preview: ({ tabId }) => captureTab(tabId),
  /** Capture and submit; resolves to the cached run state. */
  send: ({ tabId, meta }) => submitCapture(tabId, meta),
  /** The run history, for a popup that just opened. */
  state: () => getRuns(),
  /** Re-poll one run now instead of waiting for the next alarm. */
  refresh: ({ runId }) => pollRunById(runId),
};

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const handler = HANDLERS[message?.type];
  if (!handler) return false;

  Promise.resolve(handler(message))
    .then((data) => sendResponse({ ok: true, data }))
    .catch((error) => sendResponse({ ok: false, error: String(error?.message ?? error) }));
  return true; // Keep the message channel open for the async reply.
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) void pollRuns();
  else if (alarm.name === CLEAR_BADGE_ALARM) void clearBadgeIfDoneSuccess();
});

// A worker restart (Chrome unloads idle ones) loses nothing but the alarm's
// justification — resume polling if the cached run is still unfinished.
chrome.runtime.onStartup.addListener(() => void resumePolling());
chrome.runtime.onInstalled.addListener(() => void resumePolling());

/** Inject the content script into `tabId` and return what it read. */
async function captureTab(tabId) {
  let injection;
  try {
    [injection] = await chrome.scripting.executeScript({
      target: { tabId },
      files: [CAPTURE_FILE],
    });
  } catch (error) {
    // Chrome refuses to inject into its own pages and the Web Store.
    throw new Error(`No se puede leer esta pestaña: ${error?.message ?? error}`);
  }

  // `executeScript` resolves even when the injected script itself throws, reporting
  // it per frame instead — so name that error rather than reporting a bare absence.
  const result = injection?.result;
  if (!result) {
    const failure = injection?.error?.message ?? injection?.error;
    throw new Error(
      failure
        ? `El lector falló en la página: ${failure}`
        : "La página no devolvió ninguna conversación.",
    );
  }
  return result;
}

/**
 * Capture the tab and POST it to `/api/v1/captures`, caching the queued run.
 *
 * The page is re-read here rather than trusting the popup's preview, so what gets
 * scored is the conversation as it stands at send time (the user may have kept
 * chatting while the popup was open).
 */
async function submitCapture(tabId, meta) {
  const capture = await captureTab(tabId);
  if (!capture.ok) throw new Error(capture.error);
  if (!capture.messages?.length) {
    throw new Error("No se encontraron mensajes en la conversación.");
  }

  const settings = await getSettings();
  const response = await fetch(`${settings.apiUrl}/api/v1/captures`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      platform: meta.platform,
      use_case: meta.useCase,
      // The batch this capture belongs to: every capture sent under the same label
      // joins one run, however many scenarios it names. Null when left empty — the
      // API then falls back to grouping by scenario id alone.
      run_label: meta.runLabel || null,
      conversations: [
        {
          scenario_id: meta.scenarioId,
          // Null rather than "" when the field was left empty: both mean "unknown"
          // to the API, and null is what it stores.
          model_name: meta.model || null,
          source_ref: capture.url,
          messages: capture.messages,
        },
      ],
    }),
  }).catch((error) => {
    throw new Error(`No se pudo conectar con ${settings.apiUrl}: ${error.message}`);
  });

  if (!response.ok) throw new Error(await errorDetail(response));

  const { run_id: runId, status } = await response.json();
  // The API answers with the *existing* run when the scenario id is one it already
  // knows, so this capture may be joining an entry the history already holds: merge
  // into it instead of replacing it, or the platform it was already tracking is lost.
  const existing = await getRun(runId);
  const entry = {
    // Per capture, not per run: a run reused through its `run_label` holds several
    // scenarios, so only the capture knows which one it named.
    scenarioId: meta.scenarioId,
    platform: meta.platform,
    model: meta.model,
    turns: capture.messages.length,
    sourceUrl: capture.url,
  };

  const run = {
    runId,
    status,
    error: null,
    apiUrl: settings.apiUrl,
    // The scenario that opened the run, kept for entries read by older code; every
    // scenario it holds is read with `runScenarioIds`.
    scenarioId: existing?.scenarioId ?? meta.scenarioId,
    runLabel: meta.runLabel || existing?.runLabel || null,
    useCase: meta.useCase,
    // Appended unconditionally, never deduped: the API adds one PlatformExecution
    // per capture — re-capturing the same chat on the same platform gives the
    // scenario a second execution — so anything folded away here would leave the
    // history under-reporting what the run holds.
    captures: [...runCaptures(existing), entry],
    updatedAt: new Date().toISOString(),
    progress: existing?.progress ?? null,
    platforms: existing?.platforms ?? [],
  };

  await upsertRun(run);
  await setBadge(status);
  // A prior success may have armed a badge wipe; this new run keeps the badge.
  await chrome.alarms.clear(CLEAR_BADGE_ALARM);
  await chrome.alarms.create(POLL_ALARM, { periodInMinutes: POLL_MINUTES });
  return run;
}

/**
 * Poll one run's status once and write the result back into the history.
 *
 * A transient failure (API restarting, laptop asleep) records the error on the
 * run rather than wiping it, so the next poll retries. Returns the stored run.
 */
async function pollRun(run) {
  // The API URL may have changed since submission; poll where the run was sent.
  const apiUrl = run.apiUrl ?? (await getSettings()).apiUrl;
  try {
    const response = await fetch(`${apiUrl}/api/v1/evaluations/${run.runId}`);
    if (!response.ok) throw new Error(await errorDetail(response));
    const summary = await response.json();
    const updated = {
      ...run,
      error: null,
      status: summary.status,
      progress: summary.progress ?? null,
      platforms: summary.platforms ?? [],
      updatedAt: new Date().toISOString(),
    };
    await upsertRun(updated);
    return updated;
  } catch (error) {
    const failed = { ...run, error: String(error?.message ?? error) };
    await upsertRun(failed);
    return failed;
  }
}

/**
 * Poll every non-terminal run in the history, then reflect the newest run on the
 * badge and stop the alarm once nothing is left to poll.
 */
async function pollRuns() {
  const pending = (await getRuns()).filter(
    (run) => run.runId && !TERMINAL_STATUSES.includes(run.status),
  );
  if (pending.length === 0) {
    await chrome.alarms.clear(POLL_ALARM);
    return getRuns();
  }

  // Sequential so each write lands on the list the previous one produced (upsertRun
  // read-modify-writes the whole array); a handful of runs makes this cheap enough.
  for (const run of pending) await pollRun(run);

  const runs = await getRuns();
  if (runs[0]) await setBadge(runs[0].status);

  const allTerminal = !runs.some((run) => !TERMINAL_STATUSES.includes(run.status));
  if (allTerminal) {
    await chrome.alarms.clear(POLL_ALARM);
    await scheduleBadgeClear(runs);
  } else {
    // Something is running again; keep its badge until it too finishes.
    await chrome.alarms.clear(CLEAR_BADGE_ALARM);
  }
  return runs;
}

/**
 * When the newest run finished successfully, arm a delayed badge wipe; leave a
 * failure/partial `!` in place. Re-checked when the alarm fires, so a capture
 * started in the meantime cancels it.
 */
async function scheduleBadgeClear(runs) {
  if (runs[0]?.status === "completado") {
    await chrome.alarms.create(CLEAR_BADGE_ALARM, { delayInMinutes: CLEAR_BADGE_MINUTES });
  }
}

/** Wipe the badge only if everything is still done and the newest run succeeded. */
async function clearBadgeIfDoneSuccess() {
  const runs = await getRuns();
  const allTerminal = !runs.some((run) => !TERMINAL_STATUSES.includes(run.status));
  if (allTerminal && (!runs[0] || runs[0].status === "completado")) {
    await chrome.action.setBadgeText({ text: "" });
  }
}

/** Re-poll a single run by id now (the popup's refresh buttons). */
async function pollRunById(runId) {
  const run = await getRun(runId);
  if (run?.runId && !TERMINAL_STATUSES.includes(run.status)) await pollRun(run);
  return getRuns();
}

/** Restart polling after a worker restart if any cached run is still running. */
async function resumePolling() {
  const runs = await getRuns();
  const pending = runs.filter((run) => !TERMINAL_STATUSES.includes(run.status));
  if (pending.length === 0) {
    // Everything already finished; re-arm the wipe so a persisted ✓ still clears.
    await scheduleBadgeClear(runs);
    return;
  }
  await chrome.alarms.create(POLL_ALARM, { periodInMinutes: POLL_MINUTES });
  if (runs[0]) await setBadge(runs[0].status);
}

/**
 * A readable message for a failed response.
 *
 * FastAPI returns `{"detail": ...}` where detail is a string for our own
 * `HTTPException`s but a list of field errors for a validation failure.
 */
async function errorDetail(response) {
  let detail;
  try {
    detail = (await response.json()).detail;
  } catch {
    detail = null;
  }

  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => `${(item.loc ?? []).join(".")}: ${item.msg}`).join("; ");
  }
  if (response.status === 404) {
    return "La evaluación ya no existe en la API (¿se reinició la base de datos?).";
  }
  return `La API respondió ${response.status}.`;
}

async function setBadge(status) {
  const badge = BADGE[status];
  await chrome.action.setBadgeText({ text: badge?.text ?? "" });
  if (badge) await chrome.action.setBadgeBackgroundColor({ color: badge.color });
}
