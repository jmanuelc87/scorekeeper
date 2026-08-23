import React, { useCallback, useEffect, useState } from "react";
import type { Granularity, MetricScore, Run, RunsQueryParams, ScenarioPlatformExecution, ScenarioResult, Turn } from "../types";
import { fetchRuns, startRun, resumeRun } from "../api";
import { StatusBadge } from "./StatusBadge";
import { ProgressBar } from "./ProgressBar";
import { PlatformScoreList } from "./PlatformScoreList";
import { Filters } from "./Filters";
import { RunDetailModal } from "./RunDetailModal";

/** Polling interval for in-progress runs (ms). */
const POLL_INTERVAL = 600_000;

/** Widest a run card grows, in grid columns, however many scenarios it holds. */
const MAX_CARD_COLUMNS = 3;

/**
 * Main dashboard view showing all evaluation runs.
 * Fetches from GET /api/v1/runs and displays cards with status, progress, and platform scores.
 * Deeper granularity levels show scenario results and metric scores.
 */
export function RunsDashboard() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [params, setParams] = useState<RunsQueryParams>({
    granularity: "scenario_results",
  });

  const loadRuns = useCallback(async (queryParams?: RunsQueryParams) => {
    setLoading(true);
    setError("");
    try {
      const data = await fetchRuns(queryParams ?? params);
      setRuns(data);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not load runs"
      );
    } finally {
      setLoading(false);
    }
  }, [params]);

  // Initial load
  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  // Auto-poll when there are in-progress runs
  useEffect(() => {
    const hasActive = runs.some(
      (r) => r.status === "en_cola" || r.status === "en_proceso"
    );
    if (!hasActive) return;

    const timer = setInterval(() => {
      void loadRuns();
    }, POLL_INTERVAL);

    return () => clearInterval(timer);
  }, [runs, loadRuns]);

  const handleApplyFilters = () => {
    void loadRuns(params);
  };

  const granularity = params.granularity ?? "scenario_results";

  return (
    <main className="dashboard">
      <header className="dashboard-header">
        <div>
          <p className="eyebrow">Evaluation Runs</p>
          <h1>Scorekeeper</h1>
        </div>
        <button onClick={() => void loadRuns()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </header>

      <Filters
        params={params}
        onChange={setParams}
        onApply={handleApplyFilters}
        loading={loading}
      />

      {error && <p className="notice error">{error}</p>}
      {loading && runs.length === 0 && <p className="notice">Loading runs…</p>}
      {!loading && !error && runs.length === 0 && (
        <p className="notice">
          No evaluation runs found. Ingest conversations through the API to get
          started.
        </p>
      )}

      <div className="runs-grid">
        {runs.map((run) => (
          <RunCard
            key={run.run_id}
            run={run}
            granularity={granularity}
            onStarted={() => void loadRuns()}
          />
        ))}
      </div>
    </main>
  );
}

/** A single run rendered as a card. */
function RunCard({
  run,
  granularity,
  onStarted,
}: {
  run: Run;
  granularity: Granularity;
  onStarted: () => void;
}) {
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState("");
  const [showDetail, setShowDetail] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState("");

  // A run with several scenarios earns the width to show them side by side. The span
  // is clamped to the grid's own column count, so a narrow viewport still gets one.
  const columns = Math.min(run.scenario_results?.length ?? 1, MAX_CARD_COLUMNS);

  const handleStart = async () => {
    setStarting(true);
    setStartError("");
    try {
      await startRun(run.run_id);
      onStarted();
    } catch (caught) {
      setStartError(
        caught instanceof Error ? caught.message : "Failed to start"
      );
    } finally {
      setStarting(false);
    }
  };

  const handleResume = async () => {
    setResuming(true);
    setResumeError("");
    try {
      await resumeRun(run.run_id);
      onStarted();
    } catch (caught) {
      setResumeError(
        caught instanceof Error ? caught.message : "Failed to resume"
      );
    } finally {
      setResuming(false);
    }
  };

  return (
    <>
      <article className="run-card" style={{ gridColumn: `span ${columns}` }}>
        <div className="run-card-header">
          <code className="run-id" title={run.run_id}>
            {run.run_id.slice(0, 8)}…
          </code>
          <div className="run-card-actions">
            {run.status === "ingerido" && (
              <button
                className="btn-play"
                onClick={() => void handleStart()}
                disabled={starting}
                aria-label="Start evaluation"
                title="Start evaluation"
              >
                {starting ? "…" : "▶"}
              </button>
            )}
            {(run.status === "fallido" || run.status === "parcial") && (
              <button
                className="btn-resume"
                onClick={() => void handleResume()}
                disabled={resuming}
                aria-label="Resume evaluation"
                title="Resume evaluation"
              >
                {resuming ? "…" : "↻"}
              </button>
            )}
            <button
              className="btn-detail"
              onClick={() => setShowDetail(true)}
              aria-label="View run details"
              title="View details"
            >
              🔍
            </button>
            <StatusBadge status={run.status} />
          </div>
        </div>

        {startError && <p className="inline-error">{startError}</p>}
        {resumeError && <p className="inline-error">{resumeError}</p>}

        <div className="run-card-meta">
          <time dateTime={run.created_at}>
            {new Date(run.created_at).toLocaleString()}
          </time>
        </div>

        <ProgressBar progress={run.progress} />

        <PlatformScoreList platforms={run.platforms} />

      {/* Scenario results (granularity >= scenario_results) */}
      {run.scenario_results?.length ? (
        <div className="scenarios-section">
          <h3 className="section-title">Scenarios</h3>
          {/* Side by side, scrolling sideways when the card is too narrow for them. */}
          <div className="scenarios-row">
            {run.scenario_results.map((scenario) => (
              <ScenarioCard
                key={scenario.id}
                scenario={scenario}
                showMetrics={granularity === "metric_scores"}
              />
            ))}
          </div>
        </div>
      ) : null}
    </article>

    {showDetail && (
      <RunDetailModal run={run} onClose={() => setShowDetail(false)} />
    )}
    </>
  );
}

/** A single scenario rendered within a run card, one block per platform it ran on. */
function ScenarioCard({
  scenario,
  showMetrics,
}: {
  scenario: ScenarioResult;
  showMetrics: boolean;
}) {
  return (
    <div className="scenario-card">
      <div className="scenario-header">
        <span className="scenario-name" title={scenario.scenario_id}>
          {scenario.scenario_id}
        </span>
        <StatusBadge status={scenario.status} />
      </div>
      <div className="scenario-details">
        <span className="scenario-detail">
          <strong>Use case:</strong> {scenario.use_case}
        </span>
      </div>

      {/* One row per platform that answered this scenario — the comparison. */}
      <div className="executions-section">
        {scenario.platform_executions.map((execution) => (
          <ExecutionRow
            key={execution.id}
            execution={execution}
            showMetrics={showMetrics}
          />
        ))}
      </div>
    </div>
  );
}

/** One platform's answer to a scenario: its score and, optionally, its turns. */
function ExecutionRow({
  execution,
  showMetrics,
}: {
  execution: ScenarioPlatformExecution;
  showMetrics: boolean;
}) {
  return (
    <div className="execution-row">
      <div className="execution-header">
        <span className="execution-platform">{execution.platform}</span>
        <StatusBadge status={execution.status} />
        <span className="execution-score">
          {execution.average_score !== null
            ? (execution.average_score * 100).toFixed(1) + "%"
            : "—"}
        </span>
      </div>
      {execution.model_name && (
        <span className="scenario-detail">
          <strong>Model:</strong> {execution.model_name}
        </span>
      )}

      {/* Turns & metric scores (granularity = metric_scores) */}
      {showMetrics && execution.turns?.length ? (
        <div className="turns-section">
          {execution.turns.map((turn) => (
            <TurnRow key={turn.turn_id} turn={turn} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** A single turn with its metric scores. */
function TurnRow({ turn }: { turn: Turn }) {
  return (
    <div className="turn-row">
      <div className="turn-header">
        <span className="turn-number">Turn {turn.turn_number}</span>
        <span className="turn-score">
          {turn.turn_score !== null
            ? (turn.turn_score * 100).toFixed(1) + "%"
            : "—"}
        </span>
      </div>
      {turn.metric_scores.length > 0 && (
        <ul className="metric-scores-list">
          {turn.metric_scores.map((ms) => (
            <MetricScoreItem key={ms.metric_name} metric={ms} />
          ))}
        </ul>
      )}
    </div>
  );
}

/** A single metric score badge. */
function MetricScoreItem({ metric }: { metric: MetricScore }) {
  return (
    <li className="metric-score-item">
      <span className="metric-name">{metric.metric_name}</span>
      <span className="metric-value">
        {metric.score !== null ? (metric.score * 100).toFixed(1) + "%" : "N/A"}
      </span>
      {metric.judge_model && (
        <span className="metric-meta">{metric.judge_model}</span>
      )}
      <span className="metric-meta">{metric.rubric_version}</span>
    </li>
  );
}
