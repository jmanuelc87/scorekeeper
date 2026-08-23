import React from "react";
import type { Granularity, RunsQueryParams } from "../types";

interface FiltersProps {
  params: RunsQueryParams;
  onChange: (params: RunsQueryParams) => void;
  onApply: () => void;
  loading: boolean;
}

const GRANULARITY_OPTIONS: { value: Granularity; label: string }[] = [
  { value: "platform_executions", label: "Platform Executions" },
  { value: "scenario_results", label: "Scenario Results" },
  { value: "metric_scores", label: "Metric Scores" },
];

/**
 * Filter controls for the runs dashboard: platform, date range, granularity.
 */
export function Filters({ params, onChange, onApply, loading }: FiltersProps) {
  return (
    <div className="filters">
      <div className="filter-group">
        <label htmlFor="filter-platform">Platform</label>
        <input
          id="filter-platform"
          type="text"
          placeholder="e.g. claude"
          value={params.platform ?? ""}
          onChange={(e) => onChange({ ...params, platform: e.target.value })}
        />
      </div>
      <div className="filter-group">
        <label htmlFor="filter-start">From</label>
        <input
          id="filter-start"
          type="date"
          value={params.start_date ?? ""}
          onChange={(e) => onChange({ ...params, start_date: e.target.value })}
        />
      </div>
      <div className="filter-group">
        <label htmlFor="filter-end">To</label>
        <input
          id="filter-end"
          type="date"
          value={params.end_date ?? ""}
          onChange={(e) => onChange({ ...params, end_date: e.target.value })}
        />
      </div>
      <div className="filter-group">
        <label htmlFor="filter-granularity">Granularity</label>
        <select
          id="filter-granularity"
          value={params.granularity ?? "scenario_results"}
          onChange={(e) =>
            onChange({ ...params, granularity: e.target.value as Granularity })
          }
        >
          {GRANULARITY_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>
      <button
        className="btn-apply"
        onClick={onApply}
        disabled={loading}
        aria-label="Apply filters"
      >
        {loading ? "Loading…" : "Apply"}
      </button>
    </div>
  );
}
