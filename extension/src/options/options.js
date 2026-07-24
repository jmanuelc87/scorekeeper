/**
 * Options page: where the API lives and which use case captures default to.
 *
 * Both values are persisted to `chrome.storage.local` through config.js, so they
 * stay on this browser profile alongside the host permission they depend on.
 *
 * It also owns the host permission for a non-default API URL. Chrome only grants
 * optional permissions from a user gesture, so the grant has to be requested here
 * on the save click — the service worker could never ask for it on its own.
 */

import { getSettings, requestApiPermission, setSettings, trimSlash } from "../config.js";

const ui = {
  form: document.getElementById("form"),
  apiUrl: document.getElementById("apiUrl"),
  useCase: document.getElementById("useCase"),
  test: document.getElementById("test"),
  status: document.getElementById("status"),
};

ui.form.addEventListener("submit", (event) => {
  event.preventDefault();
  void save();
});

ui.test.addEventListener("click", () => void testConnection());

void load();

async function load() {
  const settings = await getSettings();
  ui.apiUrl.value = settings.apiUrl;
  ui.useCase.value = settings.useCase;
}

async function save() {
  const apiUrl = trimSlash(ui.apiUrl.value);
  const useCase = ui.useCase.value.trim();
  if (!useCase) return report("El caso de uso no puede estar vacío.", false);

  if (!(await requestApiPermission(apiUrl))) {
    return report(
      `Sin permiso para acceder a ${apiUrl}. Acéptalo para poder enviar capturas.`,
      false,
    );
  }

  await setSettings({ apiUrl, useCase });
  // Re-read instead of trusting the inputs, so the form shows exactly what was
  // persisted (trimmed URL included) rather than what was typed.
  await load();
  report("Ajustes guardados en este navegador.", true);
}

/**
 * Ping `GET /health` so a wrong URL or a stopped API shows up here rather than as
 * a failed capture later.
 */
async function testConnection() {
  const apiUrl = trimSlash(ui.apiUrl.value);
  ui.test.disabled = true;
  try {
    if (!(await requestApiPermission(apiUrl))) {
      return report(`Sin permiso para acceder a ${apiUrl}.`, false);
    }
    const response = await fetch(`${apiUrl}/health`);
    if (!response.ok) return report(`La API respondió ${response.status}.`, false);
    report(`Conexión correcta con ${apiUrl}.`, true);
  } catch (error) {
    report(`No se pudo conectar con ${apiUrl}: ${error.message}`, false);
  } finally {
    ui.test.disabled = false;
  }
}

function report(message, ok) {
  ui.status.hidden = false;
  ui.status.textContent = message;
  ui.status.classList.toggle("error", !ok);
}
