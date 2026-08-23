import React, { useCallback, useEffect, useState } from "react";
import type { Metric, PromptSlot, PromptSlotDetail, PromptVersion } from "../types";
import { createUseCase, fetchMetrics, fetchPrompt, fetchPrompts } from "../api";

/**
 * Metrics catalog page: lists the registered metrics from GET /api/v1/metrics,
 * grouped by category. Each metric expands to its prompt slots and every version
 * of their text, and can be picked to compose a use case.
 */
export function Metrics({ onOpenPrompt }: { onOpenPrompt: (promptId: string) => void }) {
  const [metrics, setMetrics] = useState<Metric[]>([]);
  const [prompts, setPrompts] = useState<PromptSlot[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<string | null>(null);

  const loadMetrics = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      // The slots are what carries the versions, so both reads feed one list.
      const [catalog, slots] = await Promise.all([fetchMetrics(), fetchPrompts()]);
      setMetrics(catalog);
      setPrompts(slots);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not load metrics"
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadMetrics();
  }, [loadMetrics]);

  const toggle = (name: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (!next.delete(name)) {
        next.add(name);
      }
      return next;
    });
  };

  const categories = groupByCategory(metrics);

  return (
    <main className="dashboard">
      <header className="dashboard-header">
        <div>
          <p className="eyebrow">Catalog</p>
          <h1>Metrics</h1>
        </div>
      </header>

      {error && <p className="notice error">{error}</p>}
      {loading && metrics.length === 0 && (
        <p className="notice">Loading metrics…</p>
      )}
      {!loading && !error && metrics.length === 0 && (
        <p className="notice">No metrics are registered.</p>
      )}

      {metrics.length > 0 && (
        <UseCaseComposer
          selected={selected}
          onClear={() => setSelected(new Set())}
        />
      )}

      <div className="metrics-catalog">
        {categories.map(([category, entries]) => (
          <section key={category} className="metric-category">
            <h3 className="section-title">
              {category} <span className="metric-count">{entries.length}</span>
            </h3>
            <ul className="metric-list">
              {entries.map((metric) => (
                <MetricRow
                  key={metric.name}
                  metric={metric}
                  slots={prompts.filter((slot) => slot.metric === metric.name)}
                  checked={selected.has(metric.name)}
                  onToggle={() => toggle(metric.name)}
                  expanded={expanded === metric.name}
                  onExpand={() =>
                    setExpanded((current) =>
                      current === metric.name ? null : metric.name
                    )
                  }
                  onOpenPrompt={onOpenPrompt}
                />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </main>
  );
}

/**
 * Names the picked metrics and creates the use case. A use case has no update or
 * delete — a created one is final — so the form clears the selection on success.
 */
function UseCaseComposer({
  selected,
  onClear,
}: {
  selected: Set<string>;
  onClear: () => void;
}) {
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState("");

  const handleCreate = async () => {
    setCreating(true);
    setError("");
    setCreated("");
    try {
      const useCase = await createUseCase(name.trim(), [...selected]);
      setCreated(`Use case "${useCase.name}" created with ${useCase.metrics.length} metrics`);
      setName("");
      onClear();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to create");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="selection-toolbar">
      <div className="filter-group">
        <label htmlFor="use-case-name">Use case name</label>
        <input
          id="use-case-name"
          type="text"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="soporte"
        />
      </div>
      <p className="selection-hint">
        {selected.size === 0
          ? "Pick the metrics this use case is scored with."
          : `${selected.size} metric${selected.size === 1 ? "" : "s"} selected`}
      </p>
      <div className="run-card-actions">
        {selected.size > 0 && (
          <button className="btn-clear" onClick={onClear} disabled={creating}>
            Clear
          </button>
        )}
        <button
          className="btn-apply"
          onClick={() => void handleCreate()}
          disabled={creating || selected.size === 0 || name.trim() === ""}
        >
          {creating ? "Creating…" : "Create use case"}
        </button>
      </div>
      {error && <p className="inline-error">{error}</p>}
      {created && <p className="inline-success">{created}</p>}
    </div>
  );
}

/**
 * One metric in the catalog: selectable for the use case being composed, and
 * expandable to every version of the prompts it renders.
 */
function MetricRow({
  metric,
  slots,
  checked,
  onToggle,
  expanded,
  onExpand,
  onOpenPrompt,
}: {
  metric: Metric;
  slots: PromptSlot[];
  checked: boolean;
  onToggle: () => void;
  expanded: boolean;
  onExpand: () => void;
  onOpenPrompt: (promptId: string) => void;
}) {
  return (
    <li className={`metric-row ${checked ? "metric-row-selected" : ""}`}>
      <div className="metric-row-header">
        <input
          type="checkbox"
          className="turn-checkbox"
          checked={checked}
          onChange={onToggle}
          aria-label={`Select ${metric.name}`}
        />
        <button
          className="metric-row-toggle"
          onClick={onExpand}
          aria-expanded={expanded}
        >
          <span className="metric-row-caret">{expanded ? "▾" : "▸"}</span>
          <span className="metric-row-name">{metric.name}</span>
        </button>
        <span className="metric-meta">{metric.rubric_version}</span>
      </div>
      {expanded && <MetricVersions slots={slots} onOpenPrompt={onOpenPrompt} />}
    </li>
  );
}

/**
 * Every version of every prompt slot the metric renders. The list read gives the
 * slots; each slot's history needs its own read, so they load on expand.
 */
function MetricVersions({
  slots,
  onOpenPrompt,
}: {
  slots: PromptSlot[];
  onOpenPrompt: (promptId: string) => void;
}) {
  const [details, setDetails] = useState<PromptSlotDetail[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const slotIds = slots.map((slot) => slot.id).join(",");

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      setLoading(true);
      setError("");
      try {
        const loaded = await Promise.all(slots.map((slot) => fetchPrompt(slot.id)));
        if (!cancelled) setDetails(loaded);
      } catch (caught) {
        if (!cancelled) {
          setError(
            caught instanceof Error ? caught.message : "Could not load versions"
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slotIds]);

  if (slots.length === 0) {
    return <p className="detail-loading">This metric renders no prompt.</p>;
  }
  if (loading) return <p className="detail-loading">Loading versions…</p>;
  if (error) return <p className="inline-error">{error}</p>;

  return (
    <div className="metric-slots">
      {details.map((slot) => (
        <div key={slot.id} className="metric-slot">
          <div className="metric-slot-header">
            <code className="metric-slot-slug">{slot.slug}</code>
            {slot.required_variables.map((variable) => (
              <span key={variable} className="metric-slot-var">{`{${variable}}`}</span>
            ))}
            <button
              className="btn-edit-prompt"
              onClick={() => onOpenPrompt(slot.id)}
            >
              Edit prompt
            </button>
          </div>
          {slot.description && (
            <p className="metric-slot-description">{slot.description}</p>
          )}
          <ul className="metric-version-list">
            {slot.versions.map((version) => (
              <PromptVersionRow key={version.id} version={version} />
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

/** One version of a prompt slot: its status and provenance. */
function PromptVersionRow({ version }: { version: PromptVersion }) {
  return (
    <li className="metric-version">
      <div className="metric-version-header">
        <span className="metric-version-number">v{version.version}</span>
        <span className={`status-badge status-version-${version.status}`}>
          {version.status}
        </span>
        {version.is_active && <span className="status-badge status-completado">active</span>}
        <span className="metric-meta">
          {version.created_by ?? "—"} · 
          {new Date(version.created_at).toLocaleString()}
        </span>
      </div>
    </li>
  );
}

/** Group the catalog by category, both categories and metrics ordered by name. */
function groupByCategory(metrics: Metric[]): [string, Metric[]][] {
  const groups = new Map<string, Metric[]>();
  for (const metric of metrics) {
    const entries = groups.get(metric.category);
    if (entries) {
      entries.push(metric);
    } else {
      groups.set(metric.category, [metric]);
    }
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}
