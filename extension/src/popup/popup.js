/**
 * Popup: preview the open conversation, tag it, and hand it to the worker.
 *
 * All work is delegated over `chrome.runtime.sendMessage` — the popup is destroyed
 * as soon as it loses focus, so it must not own a request it needs to finish. It
 * only renders, and re-renders whenever the worker writes a new run to storage.
 */

import {
  captureLabels,
  getSettings,
  newestRun,
  normalizeChatUrl,
  RUNS_KEY,
  runCaptures,
  runScenarioIds,
  STATUS_LABELS,
  slugify,
} from "../config.js";

/** Extension page listing every run; opened from the header link. */
const EVALUATIONS_PAGE = "src/evaluations/evaluations.html";

/** Window the send button coalesces repeat presses over, in ms. */
const SUBMIT_DEBOUNCE_MS = 700;

const ui = {
  detection: document.getElementById("detection"),
  form: document.getElementById("form"),
  platform: document.getElementById("platform"),
  model: document.getElementById("model"),
  scenario: document.getElementById("scenario"),
  scenarioList: document.getElementById("scenarioList"),
  runLabel: document.getElementById("runLabel"),
  runLabelList: document.getElementById("runLabelList"),
  useCase: document.getElementById("useCase"),
  submit: document.getElementById("submit"),
  error: document.getElementById("error"),
  run: document.getElementById("run"),
  runStatus: document.getElementById("runStatus"),
  runLabelValue: document.getElementById("runLabelValue"),
  runScenario: document.getElementById("runScenario"),
  runModel: document.getElementById("runModel"),
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
/** Pending debounced send, or `null` when none is armed. */
let submitTimer = null;

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
  // Coalesce repeat presses: the send fires once, SUBMIT_DEBOUNCE_MS after the last
  // one. The button is disabled on the first press so the wait cannot read as a dead
  // click — and so a second press cannot push the timer further out.
  clearTimeout(submitTimer);
  ui.submit.disabled = true;
  submitTimer = setTimeout(() => {
    submitTimer = null;
    void submit();
  }, SUBMIT_DEBOUNCE_MS);
});

// The detail panel's refresh re-reads the open chat's run.
ui.refresh.addEventListener("click", () => {
  if (!detailRunId) return;
  void send({ type: "refresh", runId: detailRunId }).then(render).catch(showError);
});

// A refresh from the evaluations tab writes the history; mirror those writes live.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes[RUNS_KEY]) render(changes[RUNS_KEY].newValue);
});

// Nothing watches this popup's console, so an unexpected failure has to land in the
// notice rather than leaving an empty panel with no explanation.
init().catch((error) => block(String(error?.message ?? error)));

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) {
    block("No hay ninguna pestaña activa.");
    return;
  }
  activeTabId = tab.id;
  activeTabUrl = tab.url ?? null;
  // Render once the tab URL is known, so the open chat's detail panel appears. The
  // history is also what the lote field is prefilled from and what both datalists
  // are built from, so it is awaited here rather than left to resolve on its own.
  const runs = await send({ type: "state" }).catch(() => []);
  render(runs);

  let capture;
  try {
    capture = await send({ type: "preview", tabId: tab.id });
  } catch (error) {
    block(error.message);
    return;
  }

  if (!capture.ok) {
    block(capture.error);
    return;
  }
  if (!capture.messages.length) {
    block(
      `Se detectó ${capture.adapter.label}, pero no se encontraron mensajes. ` +
        "Abre una conversación y vuelve a intentarlo.",
    );
    return;
  }

  const settings = await getSettings();
  // Built and swapped in one call: clearing the notice first and appending after
  // would leave it blank — no message, and a button still blocked — if anything in
  // between threw.
  ui.detection.replaceChildren(
    `${capture.messages.length} mensajes en `,
    Object.assign(document.createElement("strong"), { textContent: capture.adapter.label }),
  );
  ui.detection.className = "notice detected";
  ui.platform.value = capture.adapter.platform;
  // Blank when the chat does not name its model (or the adapter declares none) —
  // the field stays editable so it can be supplied by hand.
  ui.model.value = capture.model ?? "";
  // The lote carries over between captures, the scenario id does not — see
  // defaultScenarioId(). Blank when the last capture named no lote.
  ui.runLabel.value = newestRun(runs)?.runLabel ?? "";
  ui.scenario.value = defaultScenarioId(capture);
  ui.useCase.value = settings.useCase;
  ui.submit.disabled = false;
  ui.scenario.focus();
  ui.scenario.select();
}

/**
 * The scenario id to start from: a slug of the chat's own title.
 *
 * Not the previous capture's id any more. Within a lote the batch name is what
 * repeats and the scenario is what changes, so offering the last scenario back
 * would be wrong on most captures. Reusing one — to compare a second platform on
 * the same task — stays one gesture: every id in the history is on the field's
 * datalist, and the field opens focused and selected.
 */
function defaultScenarioId(capture) {
  return slugify(capture.title ?? "") || capture.adapter.id;
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
        model: ui.model.value.trim(),
        scenarioId: ui.scenario.value.trim(),
        runLabel: ui.runLabel.value.trim(),
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

  renderDatalists(runs);

  const current = normalizeChatUrl(activeTabUrl);
  const run = current
    ? runs.find((entry) =>
        runCaptures(entry).some((item) => normalizeChatUrl(item.sourceUrl) === current),
      )
    : null;

  detailRunId = run?.runId ?? null;
  ui.run.hidden = !run;
  if (!run) return;

  const captures = runCaptures(run);
  const labels = captureLabels(captures);
  ui.runStatus.textContent = STATUS_LABELS[run.status] ?? run.status;
  ui.runStatus.dataset.status = run.status;
  ui.runLabelValue.textContent = run.runLabel || "—";
  // Every scenario the run holds: with a lote it groups more than one.
  ui.runScenario.textContent = [...runScenarioIds(run), ...labels].join(" · ");
  ui.runModel.textContent = formatModels(captures, labels);
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

/**
 * The scenario ids and lote names already used as the two fields' autocomplete
 * lists — how a second platform gets sent under the scenario the first one opened,
 * and how another scenario joins a lote already under way.
 */
function renderDatalists(runs) {
  fillDatalist(ui.scenarioList, runs.flatMap(runScenarioIds));
  fillDatalist(ui.runLabelList, runs.map((run) => run.runLabel).filter(Boolean));
}

function fillDatalist(list, values) {
  list.replaceChildren(
    ...[...new Set(values)].map((value) =>
      Object.assign(document.createElement("option"), { value }),
    ),
  );
}

/** Name the platform beside its model once a run holds more than one capture. */
function formatModels(captures, labels) {
  if (captures.length <= 1) return captures[0]?.model || "—";
  return captures.map((item, index) => `${labels[index]}: ${item.model || "—"}`).join(" · ");
}

// One entry per platform execution scored under the run.
function formatAverages(run) {
  const averages = (run.platforms ?? [])
    .filter((entry) => entry.average_score !== null)
    .map((entry) => `${entry.platform}: ${entry.average_score.toFixed(2)}`);
  return averages.length ? averages.join(" · ") : "—";
}

/**
 * Report why this page cannot be captured and keep the send button blocked.
 *
 * The button ships `disabled`, so this only restates that default — but stating it
 * here keeps "blocked" a property of this helper rather than of the markup alone.
 */
function block(message) {
  ui.detection.className = "notice error";
  ui.detection.textContent = message;
  ui.submit.disabled = true;
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
