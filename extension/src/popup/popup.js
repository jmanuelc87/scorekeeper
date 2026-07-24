/**
 * Popup: preview the open conversation, tag it, and hand it to the worker.
 *
 * All work is delegated over `chrome.runtime.sendMessage` — the popup is destroyed
 * as soon as it loses focus, so it must not own a request it needs to finish. It
 * only renders, and re-renders whenever the worker writes a new run to storage.
 */

import {
  RUNS_KEY,
  STATUS_LABELS,
  getSettings,
  normalizeChatUrl,
  slugify,
  timestamp,
} from "../config.js";

/** Extension page listing every run; opened from the header link. */
const EVALUATIONS_PAGE = "src/evaluations/evaluations.html";

const ui = {
  detection: document.getElementById("detection"),
  form: document.getElementById("form"),
  platform: document.getElementById("platform"),
  scenario: document.getElementById("scenario"),
  useCase: document.getElementById("useCase"),
  submit: document.getElementById("submit"),
  error: document.getElementById("error"),
  run: document.getElementById("run"),
  runStatus: document.getElementById("runStatus"),
  runScenario: document.getElementById("runScenario"),
  runProgress: document.getElementById("runProgress"),
  runAverage: document.getElementById("runAverage"),
  runError: document.getElementById("runError"),
  runId: document.getElementById("runId"),
  refresh: document.getElementById("refresh"),
  evaluations: document.getElementById("evaluations"),
  options: document.getElementById("options"),
};

/** The tab being captured, resolved once on open. */
let activeTabId = null;
/** The open tab's URL, used to match a run to "this chat". */
let activeTabUrl = null;
/** The run shown in the detail panel (the open chat's run), or `null`. */
let detailRunId = null;

ui.options.addEventListener("click", (event) => {
  event.preventDefault();
  chrome.runtime.openOptionsPage();
});

ui.evaluations.addEventListener("click", (event) => {
  event.preventDefault();
  void chrome.tabs.create({ url: chrome.runtime.getURL(EVALUATIONS_PAGE) });
});

ui.form.addEventListener("submit", (event) => {
  event.preventDefault();
  void submit();
});

// The detail panel's refresh re-polls the open chat's run.
ui.refresh.addEventListener("click", () => {
  if (!detailRunId) return;
  void send({ type: "refresh", runId: detailRunId }).then(render).catch(showError);
});

// The worker keeps polling with the popup closed; mirror those writes live.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes[RUNS_KEY]) render(changes[RUNS_KEY].newValue);
});

// Nothing watches this popup's console, so an unexpected failure has to land in the
// notice rather than leaving an empty panel with no explanation.
init().catch((error) => {
  ui.detection.className = "notice error";
  ui.detection.textContent = String(error?.message ?? error);
});

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) {
    ui.detection.textContent = "No hay ninguna pestaña activa.";
    return;
  }
  activeTabId = tab.id;
  activeTabUrl = tab.url ?? null;
  // Render once the tab URL is known, so the open chat's detail panel appears.
  void send({ type: "state" }).then(render).catch(() => {});

  let capture;
  try {
    capture = await send({ type: "preview", tabId: tab.id });
  } catch (error) {
    ui.detection.textContent = error.message;
    return;
  }

  if (!capture.ok) {
    ui.detection.textContent = capture.error;
    return;
  }
  if (!capture.messages.length) {
    ui.detection.textContent =
      `Se detectó ${capture.adapter.label}, pero no se encontraron mensajes. ` +
      "Abre una conversación y vuelve a intentarlo.";
    return;
  }

  const settings = await getSettings();
  // Built and swapped in one call: clearing the notice first and appending after
  // would leave it blank — no message, no form — if anything in between threw.
  ui.detection.replaceChildren(
    `${capture.messages.length} mensajes en `,
    Object.assign(document.createElement("strong"), { textContent: capture.adapter.label }),
  );
  ui.detection.className = "notice detected";
  ui.platform.value = capture.adapter.platform;
  ui.scenario.value = defaultScenarioId(capture);
  ui.useCase.value = settings.useCase;
  ui.form.hidden = false;
  ui.scenario.focus();
  ui.scenario.select();
}

/** A unique, readable scenario id: the chat's title plus the capture time. */
function defaultScenarioId(capture) {
  const title = slugify(capture.title ?? "");
  return [title || capture.adapter.id, timestamp()].join("-");
}

async function submit() {
  ui.error.hidden = true;
  ui.submit.disabled = true;
  ui.submit.textContent = "Enviando…";

  try {
    await send({
      type: "send",
      tabId: activeTabId,
      meta: {
        platform: ui.platform.value.trim(),
        scenarioId: ui.scenario.value.trim(),
        useCase: ui.useCase.value.trim(),
      },
    });
    render(await send({ type: "state" }));
  } catch (error) {
    showError(error);
  } finally {
    ui.submit.disabled = false;
    ui.submit.textContent = "Enviar a Scorekeeper";
  }
}

/**
 * Show the "Última evaluación" panel only for the run whose chat matches the open
 * tab; the full history lives on the Evaluaciones page. Takes the run list so the
 * same storage writes that drive that page keep this panel live too.
 */
function render(runs) {
  if (!Array.isArray(runs)) return;

  const current = normalizeChatUrl(activeTabUrl);
  const run = current
    ? runs.find((entry) => normalizeChatUrl(entry.sourceUrl) === current)
    : null;

  detailRunId = run?.runId ?? null;
  ui.run.hidden = !run;
  if (!run) return;

  ui.runStatus.textContent = STATUS_LABELS[run.status] ?? run.status;
  ui.runStatus.dataset.status = run.status;
  ui.runScenario.textContent = `${run.scenarioId} · ${run.platform}`;
  ui.runId.textContent = run.runId;
  ui.runProgress.textContent = formatProgress(run);
  ui.runAverage.textContent = formatAverages(run);
  ui.runError.hidden = !run.error;
  ui.runError.textContent = run.error ?? "";
}

function formatProgress(run) {
  return run.progress
    ? `${run.progress.done}/${run.progress.total} turnos (${Math.round(run.progress.ratio * 100)} %)`
    : "—";
}

// One entry per platform in the run; a capture only ever submits one.
function formatAverages(run) {
  const averages = (run.platforms ?? [])
    .filter((entry) => entry.average_score !== null)
    .map((entry) => `${entry.platform}: ${entry.average_score.toFixed(2)}`);
  return averages.length ? averages.join(" · ") : "—";
}

function showError(error) {
  ui.error.hidden = false;
  ui.error.textContent = error.message;
}

/** Send one message to the worker, unwrapping its `{ok, data|error}` envelope. */
async function send(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response?.ok) throw new Error(response?.error ?? "El worker no respondió.");
  return response.data;
}
