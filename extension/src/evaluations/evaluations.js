/**
 * Evaluations page: the full-width table of every run submitted from this browser.
 *
 * Like the popup it owns no network work — it asks the service worker for the run
 * history and to re-poll a run, and re-renders whenever the worker writes new
 * state to storage, so the table stays live even while scoring continues.
 */

import { RUNS_KEY, STATUS_LABELS, TERMINAL_STATUSES } from "../config.js";

const ui = {
  table: document.getElementById("table"),
  body: document.getElementById("body"),
  empty: document.getElementById("empty"),
  options: document.getElementById("options"),
};

ui.options.addEventListener("click", (event) => {
  event.preventDefault();
  chrome.runtime.openOptionsPage();
});

// One "Actualizar" per row; re-poll just that run.
ui.body.addEventListener("click", (event) => {
  const button = event.target.closest(".row-refresh");
  if (!button) return;
  button.disabled = true;
  void send({ type: "refresh", runId: button.dataset.runId })
    .then(render)
    .catch(() => {});
});

// The worker keeps polling with this page in the background; mirror those writes.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes[RUNS_KEY]) render(changes[RUNS_KEY].newValue);
});

void send({ type: "state" })
  .then(render)
  .catch(() => {});

/** Render the run history, or the empty notice when there is none. */
function render(runs) {
  if (!Array.isArray(runs)) return;
  ui.empty.hidden = runs.length > 0;
  ui.table.hidden = runs.length === 0;
  ui.body.replaceChildren(...runs.map(runRow));
}

/** One `<tr>`; running rows get their own refresh button. */
function runRow(run) {
  const scenario = cell(run.scenarioId, "cell-scenario");
  scenario.title = run.scenarioId;
  const platform = cell(run.platform);

  const status = cell();
  const badge = Object.assign(document.createElement("span"), {
    className: "badge",
    textContent: STATUS_LABELS[run.status] ?? run.status,
  });
  badge.dataset.status = run.status;
  status.append(badge);
  if (run.error) {
    status.append(
      Object.assign(document.createElement("span"), {
        className: "cell-error",
        textContent: run.error,
        title: run.error,
      }),
    );
  }

  const progress = cell(formatProgress(run));
  const average = cell(rowAverage(run));

  const source = cell(null, "cell-source");
  if (run.sourceUrl) {
    source.append(
      Object.assign(document.createElement("a"), {
        href: run.sourceUrl,
        target: "_blank",
        rel: "noreferrer",
        textContent: "Abrir chat",
      }),
    );
  } else {
    source.textContent = "—";
  }

  const action = cell(null, "cell-action");
  if (!TERMINAL_STATUSES.includes(run.status)) {
    const button = Object.assign(document.createElement("button"), {
      type: "button",
      className: "link row-refresh",
      textContent: "Actualizar",
    });
    button.dataset.runId = run.runId;
    action.append(button);
  }

  const row = document.createElement("tr");
  row.append(scenario, platform, status, progress, average, source, action);
  return row;
}

/** A `<td>` with optional text and class. */
function cell(text, className) {
  const td = document.createElement("td");
  if (text != null) td.textContent = text;
  if (className) td.className = className;
  return td;
}

function formatProgress(run) {
  return run.progress
    ? `${run.progress.done}/${run.progress.total} (${Math.round(run.progress.ratio * 100)} %)`
    : "—";
}

// One entry per platform in the run; a capture only ever submits one.
function rowAverage(run) {
  const scores = (run.platforms ?? [])
    .map((entry) => entry.average_score)
    .filter((score) => score !== null);
  return scores.length ? scores.map((score) => score.toFixed(2)).join(" · ") : "—";
}

/** Send one message to the worker, unwrapping its `{ok, data|error}` envelope. */
async function send(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response?.ok) throw new Error(response?.error ?? "El worker no respondió.");
  return response.data;
}
