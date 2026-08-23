/** Progress tracker for a run (turn-level). */
export interface RunProgress {
  done: number;
  total: number;
  ratio: number;
}

/** Status breakdown counts per scenario status. */
export interface StatusBreakdown {
  [status: string]: number;
}

/**
 * One captured conversation: a scenario as it ran on a single platform.
 * `average_score` and `status` cover this conversation alone.
 */
export interface ScenarioPlatformExecution {
  id: string;
  platform: string;
  model_name: string | null;
  status: string;
  average_score: number | null;
  started_at: string | null;
  finished_at: string | null;
  /** Present only at granularity = metric_scores. */
  turns?: Turn[];
}

/**
 * A scenario within a run (granularity >= scenario_results) — the same task
 * compared across platforms. `status` rolls up over `platform_executions`, one per
 * platform it ran on. There is deliberately no scenario-level score: the numbers
 * worth reading are the per-platform ones, side by side.
 */
export interface ScenarioResult {
  id: string;
  scenario_id: string;
  use_case: string;
  status: string;
  platform_executions: ScenarioPlatformExecution[];
}

/** A per-metric score for a turn (granularity = metric_scores). */
export interface MetricScore {
  metric_name: string;
  score: number | null;
  judge_model: string | null;
  rubric_version: string;
}

/** A turn within a scenario (granularity = metric_scores). */
export interface Turn {
  turn_id: string;
  turn_number: number;
  turn_score: number | null;
  metric_scores: MetricScore[];
}

/**
 * A turn with full content as returned by GET /api/v1/scenarios/{scenario_id}/turns.
 * The list spans every platform the scenario ran on; `platform` says which.
 */
export interface ScenarioTurn {
  turn_id: string;
  platform: string;
  turn_number: number;
  prompt: string;
  response: string;
  expected_output: string | null;
  retrieved_context_source: string | null;
  turn_score: number | null;
  metric_scores: MetricScore[];
}

/**
 * One run's rollup for one platform, grouped from its scenarios by the API.
 * Present at every granularity.
 */
export interface PlatformExecution {
  platform: string;
  average_score: number | null;
  started_at: string | null;
  finished_at: string | null;
  scenarios: number;
  status_breakdown: StatusBreakdown;
}

/** A single run as returned by GET /api/v1/runs. */
export interface Run {
  run_id: string;
  status: RunStatus;
  created_at: string;
  progress: RunProgress;
  platforms: PlatformExecution[];
  /** Present at granularity >= scenario_results; each entry names its own platform. */
  scenario_results?: ScenarioResult[];
}

/** Known run statuses. */
export type RunStatus =
  | "ingerido"
  | "en_cola"
  | "en_proceso"
  | "completado"
  | "parcial"
  | "fallido";

/** Granularity options for the /runs endpoint. */
export type Granularity =
  | "platform_executions"
  | "scenario_results"
  | "metric_scores";

/** Query parameters accepted by GET /api/v1/runs. */
export interface RunsQueryParams {
  run_id?: string;
  platform?: string;
  start_date?: string;
  end_date?: string;
  granularity?: Granularity;
}

/** A single entry within a trace step. */
export interface TraceEntry {
  label: string;
  value: number | string | null;
  justification: string | null;
  metadata: Record<string, unknown>;
}

/** A step within a metric trace. */
export interface TraceStep {
  label: string;
  summary: string | null;
  entries: TraceEntry[];
}

/** The structured trace object for a metric. */
export interface Trace {
  steps: TraceStep[];
}

/** A metric trace as returned by GET /api/v1/turns/{turn_id}/traces. */
export interface MetricTrace {
  metric_name: string;
  judge_model: string | null;
  rubric_version: string | null;
  trace: Trace;
}

/** Token usage for a single turn as returned by GET /api/v1/turns/{turn_id}/token-usage. */
export interface TurnTokenUsage {
  turn_id: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

/** A registered metric as returned by GET /api/v1/metrics. */
export interface Metric {
  name: string;
  category: string;
  weight: number;
  rubric_version: string;
}

/** A use case and the metric names it is scored with. */
export interface UseCase {
  id: string;
  name: string;
  metrics: string[];
}

/** One edit of one prompt slot; `template` is write-once. */
export interface PromptVersion {
  id: string;
  version: number;
  template: string;
  status: string;
  is_active: boolean;
  changelog: string | null;
  created_by: string | null;
  created_at: string;
  published_by: string | null;
  published_at: string | null;
}

/** A prompt slot a metric renders, as listed by GET /api/v1/prompts. */
export interface PromptSlot {
  id: string;
  metric: string;
  slug: string;
  required_variables: string[];
  description: string | null;
  active_version: PromptVersion | null;
}

/** One prompt slot with its full edit history, from GET /api/v1/prompts/{id}. */
export interface PromptSlotDetail extends Omit<PromptSlot, "active_version"> {
  versions: PromptVersion[];
}
