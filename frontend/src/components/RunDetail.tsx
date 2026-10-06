import React, { useEffect, useState } from "react";
import type { Run, ScenarioPlatformExecution, ScenarioResult, ScenarioTurn, Turn } from "../types";
import { fetchRuns, fetchScenarioTurns, updateTurnSelection } from "../api";
import { StatusBadge } from "./StatusBadge";
import { ProgressBar } from "./ProgressBar";
import { PlatformScoreList } from "./PlatformScoreList";
import { ExecutionRow, MetricScoreItem, ScenarioCard } from "./RunsDashboard";
import { TurnDetail } from "./TurnDetail";

/** Where the page stands in the run; each id narrows the one before it. */
interface RunLocation {
  scenarioId?: string;
  executionId?: string;
  turnId?: string;
}

/**
 * Run details page: the run's taxonomy one level at a time — Benchmark Run →
 * Scenario Results → Platform Executions → Turns — with a breadcrumb trail back up.
 * While the run is "ingerido" the selection toolbar sits on every level, so turns
 * unchecked across several executions go out in one update.
 */
export function RunDetail({ runId, onBack }: { runId: string; onBack: () => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [at, setAt] = useState<RunLocation>({});
  const [selection, setSelection] = useState<Record<string, boolean>>({});
  const [initialSelection, setInitialSelection] = useState<Record<string, boolean>>({});
  const [updating, setUpdating] = useState(false);
  const [updateMsg, setUpdateMsg] = useState("");
  const [updateError, setUpdateError] = useState("");

  // metric_scores is the granularity that lists each execution's own turns. A scenario
  // can hold several executions on one platform, so the platform name cannot.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    fetchRuns({ run_id: runId, granularity: "metric_scores" })
      .then((runs) => {
        if (cancelled) return;
        const found = runs[0] ?? null;
        setRun(found);
        if (!found) {
          setError("Run not found");
          return;
        }
        // Initialize selection from the API's is_selected flag, defaulting to true for backward compatibility.
        const all: Record<string, boolean> = {};
        for (const scenario of found.scenario_results ?? []) {
          for (const execution of scenario.platform_executions) {
            for (const turn of execution.turns ?? []) {
              all[turn.turn_id] = turn.is_selected !== false;
            }
          }
        }
        setSelection(all);
        setInitialSelection(all);
      })
      .catch((caught) => {
        if (!cancelled)
          setError(caught instanceof Error ? caught.message : "Could not load the run");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId]);

  // A new level opens at its top, not wherever the last one was scrolled to.
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [at]);

  const canEditSelection = run?.status === "ingerido";

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
        const res = await updateTurnSelection(runId, toDeselect, false);
        totalUpdated += res.updated;
      }
      if (toSelect.length > 0) {
        const res = await updateTurnSelection(runId, toSelect, true);
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

  const scenario = run?.scenario_results?.find((s) => s.id === at.scenarioId);
  const execution = scenario?.platform_executions.find((e) => e.id === at.executionId);
  const turns = execution?.turns ?? [];
  const turn = turns.find((t) => t.turn_id === at.turnId);

  // Each level down hands the title it had to the trail and takes its own.
  const trail: { label: string; go: () => void }[] = [{ label: "Runs", go: onBack }];
  let eyebrow = "Benchmark Run";
  let title = `Run ${runId.slice(0, 8)}…`;
  if (scenario) {
    trail.push({ label: title, go: () => setAt({}) });
    eyebrow = "Scenario Result";
    title = scenario.scenario_id;
  }
  if (scenario && execution) {
    trail.push({ label: title, go: () => setAt({ scenarioId: scenario.id }) });
    eyebrow = "Platform Execution";
    title = execution.platform;
  }
  if (scenario && execution && turn) {
    trail.push({
      label: title,
      go: () => setAt({ scenarioId: scenario.id, executionId: execution.id }),
    });
    eyebrow = "Turn";
    title = `Turn ${turn.turn_number}`;
  }
  const parent = trail[trail.length - 1];

  return (
    <main className="dashboard">
      <nav aria-label="Breadcrumb">
        <ol className="breadcrumbs">
          {trail.map((crumb, i) => (
            <li key={i}>
              <button className="breadcrumb-link" onClick={crumb.go}>
                {crumb.label}
              </button>
            </li>
          ))}
          <li aria-current="page">{title}</li>
        </ol>
      </nav>

      <header className="dashboard-header">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h1>{title}</h1>
        </div>
        <button onClick={parent.go}>← {parent.label}</button>
      </header>

      {error && <p className="notice error">{error}</p>}
      {loading && !run && <p className="notice">Loading run…</p>}

      {run && (
        <div className="run-detail">
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

          {!scenario && (
            <RunOverview run={run} onOpen={(scenarioId) => setAt({ scenarioId })} />
          )}
          {scenario && !execution && (
            <ScenarioView
              scenario={scenario}
              onOpen={(executionId) => setAt({ scenarioId: scenario.id, executionId })}
            />
          )}
          {scenario && execution && !turn && (
            <ExecutionView
              execution={execution}
              selection={selection}
              displaySelection={initialSelection}
              canEdit={canEditSelection}
              onToggle={toggleTurn}
              onOpen={(turnId) =>
                setAt({ scenarioId: scenario.id, executionId: execution.id, turnId })
              }
            />
          )}
          {scenario && execution && turn && (
            <TurnView
              key={scenario.id}
              scenarioId={scenario.id}
              turns={turns}
              turnId={turn.turn_id}
              selection={selection}
              canEdit={canEditSelection}
              onToggle={toggleTurn}
              onOpen={(turnId) =>
                setAt({ scenarioId: scenario.id, executionId: execution.id, turnId })
              }
            />
          )}
        </div>
      )}
    </main>
  );
}

/** The run as a whole: its progress, each platform's rollup, then its scenarios. */
function RunOverview({ run, onOpen }: { run: Run; onOpen: (scenarioId: string) => void }) {
  const scenarios = run.scenario_results ?? [];

  return (
    <>
      <section className="run-card">
        <div className="run-card-header">
          <code className="run-id">{run.run_id}</code>
          <StatusBadge status={run.status} />
        </div>
        <div className="run-card-meta">
          <time dateTime={run.created_at}>
            {new Date(run.created_at).toLocaleString()}
          </time>
        </div>
        <ProgressBar progress={run.progress} />
        <PlatformScoreList platforms={run.platforms} />
      </section>

      <section>
        <h3 className="section-title">Scenarios</h3>
        {scenarios.length === 0 && (
          <p className="notice">No scenarios available for this run.</p>
        )}
        <div className="scenarios-row">
          {scenarios.map((scenario) => (
            <CardLink key={scenario.id} onOpen={() => onOpen(scenario.id)}>
              <ScenarioCard scenario={scenario} showMetrics={false} />
            </CardLink>
          ))}
        </div>
      </section>
    </>
  );
}

/** One scenario: the use case it was scored under, then each execution side by side. */
function ScenarioView({
  scenario,
  onOpen,
}: {
  scenario: ScenarioResult;
  onOpen: (executionId: string) => void;
}) {
  return (
    <>
      <section className="run-card">
        <div className="run-card-header">
          <span className="scenario-detail">
            <strong>Use case:</strong> {scenario.use_case}
          </span>
          <StatusBadge status={scenario.status} />
        </div>
      </section>

      <section>
        <h3 className="section-title">Platform Executions</h3>
        {scenario.platform_executions.length === 0 && (
          <p className="notice">No platform executions for this scenario.</p>
        )}
        <div className="scenarios-row">
          {scenario.platform_executions.map((execution) => (
            <CardLink key={execution.id} onOpen={() => onOpen(execution.id)}>
              <div className="scenario-card">
                <ExecutionRow execution={execution} showMetrics={false} />
                <div className="scenario-details">
                  <span>
                    <strong>Turns:</strong> {execution.turns?.length ?? 0}
                  </span>
                  {execution.started_at && (
                    <span>
                      <strong>Started:</strong>{" "}
                      {new Date(execution.started_at).toLocaleString()}
                    </span>
                  )}
                  {execution.finished_at && (
                    <span>
                      <strong>Finished:</strong>{" "}
                      {new Date(execution.finished_at).toLocaleString()}
                    </span>
                  )}
                </div>
              </div>
            </CardLink>
          ))}
        </div>
      </section>
    </>
  );
}

/** One platform's conversation: its scores rolled up over the turns, then the turns. */
function ExecutionView({
  execution,
  selection,
  displaySelection,
  canEdit,
  onToggle,
  onOpen,
}: {
  execution: ScenarioPlatformExecution;
  selection: Record<string, boolean>;
  displaySelection: Record<string, boolean>;
  canEdit: boolean;
  onToggle: (turnId: string) => void;
  onOpen: (turnId: string) => void;
}) {
  const turns = execution.turns ?? [];
  const selectedCount = turns.filter((turn) => displaySelection[turn.turn_id] !== false).length;
  const selectedScored = turns.filter((turn) => displaySelection[turn.turn_id] !== false && turn.turn_score !== null).length;

  return (
    <>
      <section className="metric-category">
        <h3 className="section-title">Averaged across turns</h3>
        <div className="scenario-details">
          <span>
            <strong>Average:</strong> {formatScore(execution.average_score)}
          </span>
          <span>
            <strong>Turns scored:</strong> {selectedScored}/{selectedCount}
          </span>
          {execution.model_name && (
            <span>
              <strong>Model:</strong> {execution.model_name}
            </span>
          )}
          <StatusBadge status={execution.status} />
        </div>
        {/* Raw per-metric means, so they do not add up to the weighted average above. */}
        <ul className="metric-scores-list">
          {metricAverages(turns).map((metric) => (
            <li key={metric.metric_name} className="metric-score-item">
              <span className="metric-name">{metric.metric_name}</span>
              <span className="metric-value">
                {metric.average !== null ? formatScore(metric.average) : "N/A"}
              </span>
              <span className="metric-meta">
                {metric.scored}/{selectedCount} turns
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section className="metric-category">
        <h3 className="section-title">
          Turns <span className="metric-count">{selectedCount}</span>
        </h3>
        {turns.length === 0 && <p className="detail-loading">No turns recorded.</p>}
        <ul className="metric-list">
          {turns.map((turn) => {
            const selected = selection[turn.turn_id] !== false;
            return (
              <li
                key={turn.turn_id}
                className={`metric-row ${!selected ? "detail-turn-deselected" : ""}`}
              >
                <div className="metric-row-header">
                  {canEdit && (
                    <input
                      type="checkbox"
                      className="turn-checkbox"
                      checked={selected}
                      onChange={() => onToggle(turn.turn_id)}
                      aria-label={`Select turn ${turn.turn_number}`}
                    />
                  )}
                  <button className="metric-row-toggle" onClick={() => onOpen(turn.turn_id)}>
                    <span className="turn-number">Turn {turn.turn_number}</span>
                    <span className="metric-row-caret">→</span>
                  </button>
                  <span className="turn-score">{formatScore(turn.turn_score)}</span>
                </div>
                {turn.metric_scores.length > 0 && (
                  <ul className="metric-scores-list">
                    {turn.metric_scores.map((ms) => (
                      <MetricScoreItem key={ms.metric_name} metric={ms} />
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      </section>
    </>
  );
}

/**
 * One turn in full. Its content comes from the scenario's turns endpoint, read once
 * per visit; prev/next walk the execution's turns without reading it again.
 */
function TurnView({
  scenarioId,
  turns,
  turnId,
  selection,
  canEdit,
  onToggle,
  onOpen,
}: {
  scenarioId: string;
  turns: Turn[];
  turnId: string;
  selection: Record<string, boolean>;
  canEdit: boolean;
  onToggle: (turnId: string) => void;
  onOpen: (turnId: string) => void;
}) {
  const [content, setContent] = useState<ScenarioTurn[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    fetchScenarioTurns(scenarioId)
      .then((data) => {
        if (!cancelled) setContent(data);
      })
      .catch((caught) => {
        if (!cancelled)
          setError(caught instanceof Error ? caught.message : "Failed to load the turn");
      });
    return () => {
      cancelled = true;
    };
  }, [scenarioId]);

  const index = turns.findIndex((t) => t.turn_id === turnId);
  const prev = index > 0 ? turns[index - 1] : undefined;
  const next = turns[index + 1];
  const turn = content?.find((t) => t.turn_id === turnId);

  return (
    <>
      <div className="turn-pager">
        <button onClick={() => prev && onOpen(prev.turn_id)} disabled={!prev}>
          ← {prev ? `Turn ${prev.turn_number}` : "Previous"}
        </button>
        <button onClick={() => next && onOpen(next.turn_id)} disabled={!next}>
          {next ? `Turn ${next.turn_number}` : "Next"} →
        </button>
      </div>

      {error && <p className="inline-error">{error}</p>}
      {!content && !error && <p className="detail-loading">Loading turn…</p>}
      {content && !turn && <p className="notice">Turn not found.</p>}
      {/* Keyed so a turn's opened traces and token usage never carry to the next. */}
      {turn && (
        <TurnDetail
          key={turn.turn_id}
          turn={turn}
          selected={selection[turn.turn_id] !== false}
          canEdit={canEdit}
          onToggle={() => onToggle(turn.turn_id)}
        />
      )}
    </>
  );
}

/** A card that opens the next level down, on click, Enter or Space. */
function CardLink({ onOpen, children }: { onOpen: () => void; children: React.ReactNode }) {
  return (
    <div
      className="card-link"
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
    >
      {children}
    </div>
  );
}

/** A 0–1 score as a percentage, or a dash when there is none. */
function formatScore(score: number | null): string {
  return score !== null ? (score * 100).toFixed(1) + "%" : "—";
}

/**
 * Each metric's mean over the turns that scored it, in the order the metrics first
 * appear. `scored` counts those turns; `average` is null when none did.
 */
function metricAverages(
  turns: Turn[]
): { metric_name: string; average: number | null; scored: number }[] {
  const totals = new Map<string, { sum: number; scored: number }>();
  for (const turn of turns) {
    for (const ms of turn.metric_scores) {
      const total = totals.get(ms.metric_name) ?? { sum: 0, scored: 0 };
      if (ms.score !== null) {
        total.sum += ms.score;
        total.scored += 1;
      }
      totals.set(ms.metric_name, total);
    }
  }
  return [...totals].map(([metric_name, { sum, scored }]) => ({
    metric_name,
    average: scored > 0 ? sum / scored : null,
    scored,
  }));
}
