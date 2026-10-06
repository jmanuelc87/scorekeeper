import type { AuthProvider, AuthProviderInput, Metric, MetricTrace, PromptSlot, PromptSlotDetail, PromptVersion, Run, RunsQueryParams, ScenarioTurn, TurnTokenUsage, UseCase } from "./types";

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8001";

/**
 * Fetch runs from GET /api/v1/runs with optional filters.
 * Returns the array of runs or throws on network/HTTP errors.
 */
export async function fetchRuns(params?: RunsQueryParams): Promise<Run[]> {
  const url = new URL(`${API_URL}/api/v1/runs`);

  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") {
        url.searchParams.set(key, value);
      }
    }
  }

  const response = await fetch(url.toString());

  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<Run[]>;
}

/**
 * Start scoring an ingested run via POST /api/v1/evaluations/{run_id}/start.
 * The run must be in status "ingerido". Returns the updated status.
 */
export async function startRun(runId: string): Promise<{ run_id: string; status: string }> {
  const response = await fetch(`${API_URL}/api/v1/evaluations/${runId}/start`, {
    method: "POST",
  });

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("Run not found");
    }
    if (response.status === 409) {
      throw new Error("Run has already been started");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<{ run_id: string; status: string }>;
}

/**
 * Fetch a scenario's turns with full content and metric scores
 * via GET /api/v1/scenarios/{scenario_id}/turns.
 */
export async function fetchScenarioTurns(scenarioId: string): Promise<ScenarioTurn[]> {
  const response = await fetch(`${API_URL}/api/v1/scenarios/${scenarioId}/turns`);

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("Scenario not found");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<ScenarioTurn[]>;
}

/**
 * Update turn selection for a run via PATCH /api/v1/evaluations/{run_id}/turns/selection.
 * Only works while the run is in "ingerido" status.
 */
export async function updateTurnSelection(
  runId: string,
  turnIds: string[],
  isSelected: boolean
): Promise<{ run_id: string; updated: number }> {
  const response = await fetch(
    `${API_URL}/api/v1/evaluations/${runId}/turns/selection`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turn_ids: turnIds, is_selected: isSelected }),
    }
  );

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("Run not found");
    }
    if (response.status === 409) {
      throw new Error("Selection is frozen — run has already been started");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<{ run_id: string; updated: number }>;
}

/**
 * Resume a failed evaluation via POST /api/v1/evaluations/{run_id}/resume.
 * The run must be in a failed/partial terminal state.
 */
export async function resumeRun(runId: string): Promise<{ run_id: string; status: string }> {
  const response = await fetch(`${API_URL}/api/v1/evaluations/${runId}/resume`, {
    method: "POST",
  });

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("Run not found");
    }
    if (response.status === 409) {
      throw new Error("Run cannot be resumed in its current state");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<{ run_id: string; status: string }>;
}

/**
 * Re-run a completed evaluation via POST /api/v1/evaluations/{run_id}/rerun.
 * Drops the run's scores and judges every selected turn again under the currently
 * active prompts. The run must be in the "completado" state.
 */
export async function rerunRun(runId: string): Promise<{ run_id: string; status: string }> {
  const response = await fetch(`${API_URL}/api/v1/evaluations/${runId}/rerun`, {
    method: "POST",
  });

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("Run not found");
    }
    if (response.status === 409) {
      throw new Error("Only a completed run can be re-run");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<{ run_id: string; status: string }>;
}

/**
 * Fetch the structured metric traces for a turn via GET /api/v1/turns/{turn_id}/traces.
 * Returns one entry per metric scored on the turn.
 */
export async function fetchTurnTraces(turnId: string): Promise<MetricTrace[]> {
  const response = await fetch(`${API_URL}/api/v1/turns/${turnId}/traces`);

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("Turn not found");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<MetricTrace[]>;
}

/**
 * Fetch LLM token usage for a single turn via GET /api/v1/turns/{turn_id}/token-usage.
 */
export async function fetchTurnTokenUsage(turnId: string): Promise<TurnTokenUsage> {
  const response = await fetch(`${API_URL}/api/v1/turns/${turnId}/token-usage`);

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("Turn not found");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<TurnTokenUsage>;
}

/**
 * Fetch the registered metric catalog via GET /api/v1/metrics.
 * Returns the metrics ordered by name.
 */
export async function fetchMetrics(): Promise<Metric[]> {
  const response = await fetch(`${API_URL}/api/v1/metrics`);

  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<Metric[]>;
}

/**
 * Create a use case via POST /api/v1/use-cases — a name plus the metrics it is
 * scored with. There is no update or delete: to score differently, create another.
 */
export async function createUseCase(name: string, metrics: string[]): Promise<UseCase> {
  const response = await fetch(`${API_URL}/api/v1/use-cases`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, metrics }),
  });

  if (!response.ok) {
    if (response.status === 409) {
      throw new Error(`A use case named "${name}" already exists`);
    }
    if (response.status === 422) {
      throw new Error("Name is required and at least one metric must be picked");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<UseCase>;
}

/**
 * Update a use case's metrics via PUT /api/v1/use-cases/{id}.
 */
export async function updateUseCase(id: string, metrics: string[]): Promise<UseCase> {
  const response = await fetch(`${API_URL}/api/v1/use-cases/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ metrics }),
  });

  if (!response.ok) {
    const detail = await errorMessage(response);
    if (response.status === 404) {
      throw new Error("Use case not found");
    }
    if (response.status === 409) {
      throw new Error(detail || "Cannot edit this use case");
    }
    if (response.status === 422) {
      throw new Error(detail || "At least one metric must be picked");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<UseCase>;
}

/**
 * List every use case with its metric names via GET /api/v1/use-cases.
 * `default` comes back with an empty `metrics` list — it scores every
 * registered metric rather than a composed subset.
 */
export async function fetchUseCases(): Promise<UseCase[]> {
  const response = await fetch(`${API_URL}/api/v1/use-cases`);

  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<UseCase[]>;
}

/**
 * Fetch every prompt slot the registered metrics render via GET /api/v1/prompts.
 * Each entry carries the version currently active, or null when none is published.
 */
export async function fetchPrompts(): Promise<PromptSlot[]> {
  const response = await fetch(`${API_URL}/api/v1/prompts`);

  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<PromptSlot[]>;
}

/**
 * Fetch one prompt slot with its full version history via GET /api/v1/prompts/{prompt_id}.
 * Versions come newest first, and their numbers have gaps where drafts were discarded.
 */
export async function fetchPrompt(promptId: string): Promise<PromptSlotDetail> {
  const response = await fetch(`${API_URL}/api/v1/prompts/${promptId}`);

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("Prompt not found");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<PromptSlotDetail>;
}

/**
 * Open a new draft of a prompt slot via POST /api/v1/prompts/{prompt_id}/versions.
 * `template` is write-once, so a re-save replaces the slot's open draft.
 */
export async function createPromptVersion(
  promptId: string,
  template: string,
  changelog: string | null,
  author: string | null
): Promise<PromptVersion> {
  const response = await fetch(`${API_URL}/api/v1/prompts/${promptId}/versions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ template, changelog, author }),
  });

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error("Prompt not found");
    }
    if (response.status === 422) {
      throw new Error("The template cannot be empty");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<PromptVersion>;
}

/**
 * Validate a draft and make it the live version via
 * POST /api/v1/prompts/{prompt_id}/versions/{version_id}/publish.
 * A 422 names the variables the template got wrong, so it is surfaced verbatim.
 */
export async function publishPromptVersion(
  promptId: string,
  versionId: string,
  author: string | null
): Promise<PromptVersion> {
  const response = await fetch(
    `${API_URL}/api/v1/prompts/${promptId}/versions/${versionId}/publish`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ author }),
    }
  );

  if (!response.ok) {
    if (response.status === 422) {
      const body = (await response.json().catch(() => null)) as { detail?: string } | null;
      throw new Error(body?.detail ?? "The template does not match the slot's variables");
    }
    if (response.status === 409) {
      throw new Error("Only a draft can be published");
    }
    if (response.status === 404) {
      throw new Error("Prompt version not found");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<PromptVersion>;
}

/**
 * Abandon an open draft via
 * POST /api/v1/prompts/{prompt_id}/versions/{version_id}/discard.
 * The row and its version number are kept — nothing is deleted.
 */
export async function discardPromptVersion(
  promptId: string,
  versionId: string
): Promise<PromptVersion> {
  const response = await fetch(
    `${API_URL}/api/v1/prompts/${promptId}/versions/${versionId}/discard`,
    { method: "POST" }
  );

  if (!response.ok) {
    if (response.status === 409) {
      throw new Error("Only a draft can be discarded");
    }
    if (response.status === 404) {
      throw new Error("Prompt version not found");
    }
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<PromptVersion>;
}

/**
 * List the configured credential providers via GET /api/v1/auth-providers.
 * The stored certificate private key is never returned — only `has_private_key`.
 */
export async function fetchAuthProviders(): Promise<AuthProvider[]> {
  const response = await fetch(`${API_URL}/api/v1/auth-providers`);

  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }

  return response.json() as Promise<AuthProvider[]>;
}

/**
 * Create a credential provider via POST /api/v1/auth-providers.
 * A 422 names the unknown provider kind or the missing encryption key, so it is
 * surfaced verbatim.
 */
export async function createAuthProvider(body: AuthProviderInput): Promise<AuthProvider> {
  const response = await fetch(`${API_URL}/api/v1/auth-providers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }

  return response.json() as Promise<AuthProvider>;
}

/**
 * Partially update a credential provider via PATCH /api/v1/auth-providers/{id}.
 * Only the keys present in `body` are changed.
 */
export async function updateAuthProvider(
  providerId: string,
  body: AuthProviderInput
): Promise<AuthProvider> {
  const response = await fetch(`${API_URL}/api/v1/auth-providers/${providerId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }

  return response.json() as Promise<AuthProvider>;
}

/** Delete a credential provider via DELETE /api/v1/auth-providers/{id}. */
export async function deleteAuthProvider(providerId: string): Promise<void> {
  const response = await fetch(`${API_URL}/api/v1/auth-providers/${providerId}`, {
    method: "DELETE",
  });

  if (!response.ok) {
    throw new Error(await errorMessage(response));
  }
}

/**
 * The API's `detail` for a failed credential-store call, which names the offending
 * field or the conflicting (provider, host) pair, falling back to the status code.
 */
async function errorMessage(response: Response): Promise<string> {
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  const detail = body?.detail;
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    // FastAPI's own 422 shape: a list of {loc, msg} validation errors.
    return detail.map((item) => (item as { msg?: string }).msg ?? "Invalid value").join("; ");
  }
  return `Request failed (${response.status})`;
}
