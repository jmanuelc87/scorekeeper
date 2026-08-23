import React, { useCallback, useEffect, useState } from "react";
import type { MetricScore, MetricTrace, Run, ScenarioPlatformExecution, ScenarioTurn, TraceEntry, TraceStep, TurnTokenUsage } from "../types";
import { fetchScenarioTurns, fetchTurnTraces, fetchTurnTokenUsage, updateTurnSelection } from "../api";
import { StatusBadge } from "./StatusBadge";

interface RunDetailModalProps {
  run: Run;
  onClose: () => void;
}

/**
 * Modal overlay showing full turn content and metric scores for a run's scenarios.
 * Includes checkboxes to select/deselect turns for scoring and an Update Selection button
 * (only active when run is in "ingerido" status).
 */
export function RunDetailModal({ run, onClose }: RunDetailModalProps) {
  const [selection, setSelection] = useState<Record<string, boolean>>({});
  const [initialSelection, setInitialSelection] = useState<Record<string, boolean>>({});
  const [updating, setUpdating] = useState(false);
  const [updateMsg, setUpdateMsg] = useState("");
  const [updateError, setUpdateError] = useState("");

  const canEditSelection = run.status === "ingerido";

  // Close on Escape key
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose]);

  // Prevent body scroll while modal is open
  useEffect(() => {
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = "";
    };
  }, []);

  /** Called by ScenarioDetail when turns are loaded — registers them in selection state. */
  const registerTurns = useCallback((turns: ScenarioTurn[]) => {
    setSelection((prev) => {
      const next = { ...prev };
      for (const t of turns) {
        if (!(t.turn_id in next)) {
          next[t.turn_id] = true; // all selected by default
        }
      }
      return next;
    });
    setInitialSelection((prev) => {
      const next = { ...prev };
      for (const t of turns) {
        if (!(t.turn_id in next)) {
          next[t.turn_id] = true;
        }
      }
      return next;
    });
  }, []);

  const toggleTurn = (turnId: string) => {
    setSelection((prev) => ({ ...prev, [turnId]: !prev[turnId] }));
  };

  const hasChanges = Object.keys(selection).some(
    (id) => selection[id] !== initialSelection[id]
  );

  const handleUpdateSelection = async () => {
    setUpdating(true);
    setUpdateMsg("");
    setUpdateError("");

    // Split into turns to select and turns to deselect
    const toSelect: string[] = [];
    const toDeselect: string[] = [];
    for (const [id, selected] of Object.entries(selection)) {
      if (selected !== initialSelection[id]) {
        if (selected) {
          toSelect.push(id);
        } else {
          toDeselect.push(id);
        }
      }
    }

    try {
      let totalUpdated = 0;
      if (toDeselect.length > 0) {
        const res = await updateTurnSelection(run.run_id, toDeselect, false);
        totalUpdated += res.updated;
      }
      if (toSelect.length > 0) {
        const res = await updateTurnSelection(run.run_id, toSelect, true);
        totalUpdated += res.updated;
      }
      setUpdateMsg(`Selection updated (${totalUpdated} turn${totalUpdated !== 1 ? "s" : ""} changed)`);
      // Sync initial to current so hasChanges resets
      setInitialSelection({ ...selection });
    } catch (caught) {
      setUpdateError(
        caught instanceof Error ? caught.message : "Failed to update selection"
      );
    } finally {
      setUpdating(false);
    }
  };

  const scenarios = run.scenario_results ?? [];

  return (
    <div className="modal-backdrop" onClick={onClose} role="dialog" aria-modal="true" aria-label="Run details">
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2 className="modal-title">
              Run <code>{run.run_id.slice(0, 8)}…</code>
            </h2>
            <span className="modal-subtitle">
              {new Date(run.created_at).toLocaleString()} · <StatusBadge status={run.status} />
            </span>
          </div>
          <button className="btn-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        <div className="modal-body">
          {canEditSelection && (
            <div className="selection-toolbar">
              <p className="selection-hint">
                Uncheck turns you don't want scored, then click Update Selection.
              </p>
              <div className="selection-actions">
                {updateMsg && <span className="selection-success">{updateMsg}</span>}
                {updateError && <span className="inline-error">{updateError}</span>}
                <button
                  className="btn-update-selection"
                  onClick={() => void handleUpdateSelection()}
                  disabled={updating || !hasChanges}
                >
                  {updating ? "Updating…" : "Update Selection"}
                </button>
              </div>
            </div>
          )}

          {scenarios.length === 0 && (
            <p className="notice">No scenarios available for this run.</p>
          )}
          {scenarios.map((scenario) => (
            <ScenarioDetail
              key={scenario.id}
              scenarioId={scenario.id}
              scenarioLabel={scenario.scenario_id}
              executions={scenario.platform_executions}
              useCase={scenario.use_case}
              status={scenario.status}
              selection={selection}
              canEdit={canEditSelection}
              onToggle={toggleTurn}
              onTurnsLoaded={registerTurns}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

/** Fetches and renders turns for one scenario. */
function ScenarioDetail({
  scenarioId,
  scenarioLabel,
  executions,
  useCase,
  status,
  selection,
  canEdit,
  onToggle,
  onTurnsLoaded,
}: {
  scenarioId: string;
  scenarioLabel: string;
  executions: ScenarioPlatformExecution[];
  useCase: string;
  status: string;
  selection: Record<string, boolean>;
  canEdit: boolean;
  onToggle: (turnId: string) => void;
  onTurnsLoaded: (turns: ScenarioTurn[]) => void;
}) {
  const [turns, setTurns] = useState<ScenarioTurn[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    fetchScenarioTurns(scenarioId)
      .then((data) => {
        if (!cancelled) {
          setTurns(data);
          onTurnsLoaded(data);
        }
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "Failed to load turns");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scenarioId, onTurnsLoaded]);

  return (
    <section className="detail-scenario">
      <div className="detail-scenario-header">
        <h3 className="detail-scenario-title">{scenarioLabel}</h3>
        <StatusBadge status={status} />
      </div>
      <div className="detail-scenario-meta">
        <span><strong>Use case:</strong> {useCase}</span>
      </div>
      {/* One line per platform that answered — these scores are the comparison. */}
      <div className="detail-scenario-platforms">
        {executions.map((execution) => (
          <span key={execution.id} className="detail-scenario-platform">
            <strong>{execution.platform}</strong>
            {execution.model_name ? ` (${execution.model_name})` : ""}:{" "}
            {execution.average_score !== null
              ? (execution.average_score * 100).toFixed(1) + "%"
              : "—"}
          </span>
        ))}
      </div>

      {loading && <p className="detail-loading">Loading turns…</p>}
      {error && <p className="inline-error">{error}</p>}

      {!loading && !error && turns.length === 0 && (
        <p className="detail-loading">No turns recorded.</p>
      )}

      {turns.map((turn) => (
        <div
          key={turn.turn_id}
          className={`detail-turn ${selection[turn.turn_id] === false ? "detail-turn-deselected" : ""}`}
        >
          <div className="detail-turn-header">
            <div className="detail-turn-left">
              {canEdit && (
                <input
                  type="checkbox"
                  className="turn-checkbox"
                  checked={selection[turn.turn_id] !== false}
                  onChange={() => onToggle(turn.turn_id)}
                  aria-label={`Select turn ${turn.turn_number}`}
                />
              )}
              <span className="turn-number">
                {turn.platform} · Turn {turn.turn_number}
              </span>
            </div>
            <span className="turn-score">
              {turn.turn_score !== null
                ? (turn.turn_score * 100).toFixed(1) + "%"
                : "—"}
            </span>
          </div>

          <div className="detail-turn-content">
            <div className="detail-message detail-prompt">
              <span className="detail-role">User</span>
              <p className="detail-text">{turn.prompt}</p>
            </div>
            <div className="detail-message detail-response">
              <span className="detail-role">Assistant</span>
              <p className="detail-text">{turn.response}</p>
            </div>
            {turn.expected_output && (
              <div className="detail-message detail-expected">
                <span className="detail-role">Expected</span>
                <p className="detail-text">{turn.expected_output}</p>
              </div>
            )}
            {turn.retrieved_context_source && (
              <div className="detail-message detail-context">
                <span className="detail-role">Retrieved Context</span>
                <p className="detail-text">{turn.retrieved_context_source}</p>
              </div>
            )}
          </div>

          {turn.metric_scores.length > 0 && (
            <div className="detail-metrics">
              <span className="detail-metrics-label">Metrics</span>
              <MetricScoresList turnId={turn.turn_id} scores={turn.metric_scores} />
            </div>
          )}

          <TokenUsageDisplay turnId={turn.turn_id} />
        </div>
      ))}
    </section>
  );
}

/** Displays token usage for a turn, fetched on demand. */
function TokenUsageDisplay({ turnId }: { turnId: string }) {
  const [usage, setUsage] = useState<TurnTokenUsage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [visible, setVisible] = useState(false);

  const handleToggle = async () => {
    if (visible) {
      setVisible(false);
      return;
    }
    setVisible(true);
    if (!usage && !loading) {
      setLoading(true);
      setError("");
      try {
        const data = await fetchTurnTokenUsage(turnId);
        setUsage(data);
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Failed to load token usage"
        );
      } finally {
        setLoading(false);
      }
    }
  };

  return (
    <div className="token-usage-section">
      <button
        className="btn-token-usage"
        onClick={() => void handleToggle()}
        aria-expanded={visible}
        aria-label="Toggle token usage"
      >
        <span className="token-usage-icon">⚡</span>
        Token Usage
        <span className="metric-expand-icon">{visible ? "▾" : "▸"}</span>
      </button>

      {visible && loading && <p className="detail-loading">Loading token usage…</p>}
      {visible && error && <p className="inline-error">{error}</p>}

      {visible && usage && (
        <div className="token-usage-panel">
          <div className="token-usage-item">
            <span className="token-usage-label">Input</span>
            <span className="token-usage-value">{usage.input_tokens.toLocaleString()}</span>
          </div>
          <div className="token-usage-item">
            <span className="token-usage-label">Output</span>
            <span className="token-usage-value">{usage.output_tokens.toLocaleString()}</span>
          </div>
          <div className="token-usage-item token-usage-total">
            <span className="token-usage-label">Total</span>
            <span className="token-usage-value">{usage.total_tokens.toLocaleString()}</span>
          </div>
        </div>
      )}
    </div>
  );
}

/** List of metric scores for a turn — clicking one fetches and shows its trace. */
function MetricScoresList({ turnId, scores }: { turnId: string; scores: MetricScore[] }) {
  const [traces, setTraces] = useState<MetricTrace[] | null>(null);
  const [loadingTraces, setLoadingTraces] = useState(false);
  const [traceError, setTraceError] = useState("");
  const [expandedMetric, setExpandedMetric] = useState<string | null>(null);

  const handleMetricClick = async (metricName: string) => {
    // Toggle off if already expanded
    if (expandedMetric === metricName) {
      setExpandedMetric(null);
      return;
    }

    setExpandedMetric(metricName);

    // Fetch traces if not already loaded for this turn
    if (!traces) {
      setLoadingTraces(true);
      setTraceError("");
      try {
        const data = await fetchTurnTraces(turnId);
        setTraces(data);
      } catch (caught) {
        setTraceError(
          caught instanceof Error ? caught.message : "Failed to load traces"
        );
      } finally {
        setLoadingTraces(false);
      }
    }
  };

  const getTraceForMetric = (metricName: string): MetricTrace | undefined => {
    return traces?.find((t) => t.metric_name === metricName);
  };

  return (
    <div className="metric-scores-with-traces">
      <ul className="metric-scores-list">
        {scores.map((ms) => (
          <li
            key={ms.metric_name}
            className={`metric-score-item metric-score-clickable ${expandedMetric === ms.metric_name ? "metric-score-active" : ""}`}
            onClick={() => void handleMetricClick(ms.metric_name)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                void handleMetricClick(ms.metric_name);
              }
            }}
            aria-expanded={expandedMetric === ms.metric_name}
            aria-label={`View trace for ${ms.metric_name}`}
          >
            <span className="metric-name">{ms.metric_name}</span>
            <span className="metric-value">
              {ms.score !== null ? (ms.score * 100).toFixed(1) + "%" : "N/A"}
            </span>
            {ms.judge_model && (
              <span className="metric-meta">{ms.judge_model}</span>
            )}
            <span className="metric-meta">{ms.rubric_version}</span>
            <span className="metric-expand-icon">
              {expandedMetric === ms.metric_name ? "▾" : "▸"}
            </span>
          </li>
        ))}
      </ul>

      {traceError && <p className="inline-error">{traceError}</p>}

      {expandedMetric && loadingTraces && (
        <div className="trace-panel">
          <p className="detail-loading">Loading trace…</p>
        </div>
      )}

      {expandedMetric && !loadingTraces && !traceError && (
        <TracePanel trace={getTraceForMetric(expandedMetric)} metricName={expandedMetric} />
      )}
    </div>
  );
}

/** Renders the structured trace for a single metric. */
function TracePanel({ trace, metricName }: { trace: MetricTrace | undefined; metricName: string }) {
  if (!trace) {
    return (
      <div className="trace-panel">
        <p className="detail-loading">No trace available for {metricName}.</p>
      </div>
    );
  }

  return (
    <div className="trace-panel">
      <div className="trace-header">
        <span className="trace-title">Trace: {trace.metric_name}</span>
        {trace.judge_model && <span className="trace-meta">{trace.judge_model}</span>}
        {trace.rubric_version && <span className="trace-meta">{trace.rubric_version}</span>}
      </div>
      {trace.trace.steps.map((step, i) => (
        <TraceStepView key={i} step={step} />
      ))}
    </div>
  );
}

/** Renders a single step within a trace. */
function TraceStepView({ step }: { step: TraceStep }) {
  return (
    <div className="trace-step">
      <div className="trace-step-header">
        <span className="trace-step-label">{step.label}</span>
        {step.summary && <span className="trace-step-summary">{step.summary}</span>}
      </div>
      {step.entries.map((entry, i) => (
        <TraceEntryView key={i} entry={entry} />
      ))}
    </div>
  );
}

/** Renders a single entry within a trace step. */
function TraceEntryView({ entry }: { entry: TraceEntry }) {
  return (
    <div className="trace-entry">
      <div className="trace-entry-header">
        <span className="trace-entry-label">{entry.label}</span>
        {entry.value !== null && (
          <span className="trace-entry-value">
            {typeof entry.value === "number" ? entry.value.toFixed(2) : entry.value}
          </span>
        )}
      </div>
      {entry.justification && (
        <p className="trace-entry-justification">{entry.justification}</p>
      )}
      {entry.metadata && Object.keys(entry.metadata).length > 0 && (
        <pre className="trace-entry-metadata">
          {JSON.stringify(entry.metadata, null, 2)}
        </pre>
      )}
    </div>
  );
}
