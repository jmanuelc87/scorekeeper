import React, { useState } from "react";
import type { JudgeCall, MetricScore, MetricTrace, ScenarioTurn, TraceEntry, TraceStep, TurnTokenUsage } from "../types";
import { fetchTurnTraces, fetchTurnTokenUsage } from "../api";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * One turn in full: the exchange, its metric scores (each opening its trace) and its
 * token usage. The checkbox selects the turn for scoring and only shows while the
 * run's selection can still be edited.
 */
export function TurnDetail({
  turn,
  selected,
  canEdit,
  onToggle,
}: {
  turn: ScenarioTurn;
  selected: boolean;
  canEdit: boolean;
  onToggle: () => void;
}) {
  return (
    <div className={`detail-turn ${!selected ? "detail-turn-deselected" : ""}`}>
      <div className="detail-turn-header">
        <div className="detail-turn-left">
          {canEdit && (
            <input
              type="checkbox"
              className="turn-checkbox"
              checked={selected}
              onChange={onToggle}
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
          <div className="detail-text detail-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.prompt}</ReactMarkdown>
          </div>
        </div>
        <div className="detail-message detail-response">
          <span className="detail-role">Assistant</span>
          <div className="detail-text detail-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.response}</ReactMarkdown>
          </div>
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
      <JudgeCallsPanel calls={trace.judge_calls} />
    </div>
  );
}

/** Renders the LLM calls the judge made for a metric, each one collapsible. */
function JudgeCallsPanel({ calls }: { calls: JudgeCall[] | undefined }) {
  const [expanded, setExpanded] = useState<number | null>(null);

  if (!calls || calls.length === 0) {
    return null;
  }

  const toggle = (sequence: number) => {
    setExpanded(expanded === sequence ? null : sequence);
  };

  return (
    <div className="judge-calls">
      <span className="judge-calls-title">Llamadas al juez ({calls.length})</span>
      {calls.map((call) => (
        <div className="judge-call" key={call.sequence}>
          <div
            className="judge-call-header"
            onClick={() => toggle(call.sequence)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                toggle(call.sequence);
              }
            }}
            aria-expanded={expanded === call.sequence}
            aria-label={`Ver llamada ${call.sequence}`}
          >
            <span className="judge-call-sequence">#{call.sequence}</span>
            {call.step && <span className="judge-call-meta">{call.step}</span>}
            <span className="judge-call-meta">{call.model}</span>
            <span className="judge-call-meta">{call.latency_ms} ms</span>
            <span className="metric-expand-icon">
              {expanded === call.sequence ? "▾" : "▸"}
            </span>
          </div>
          {expanded === call.sequence && (
            <div className="judge-call-body">
              <span className="judge-call-label">System prompt</span>
              <p className="judge-call-text">{call.system_prompt}</p>
              <span className="judge-call-label">Prompt</span>
              <p className="judge-call-text">{call.prompt}</p>
            </div>
          )}
        </div>
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
