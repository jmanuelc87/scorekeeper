import React, { useCallback, useEffect, useState } from "react";
import type { UseCase } from "../types";
import { fetchUseCases } from "../api";

/**
 * Use cases page: lists every use case from GET /api/v1/use-cases with the metric
 * set it scores with. There is no update or delete — a use case a run points at is
 * what makes that run's scores readable — so this screen is read-only. Compose a
 * new one from the Metrics catalog.
 */
export function UseCases() {
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const loadUseCases = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setUseCases(await fetchUseCases());
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not load use cases"
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadUseCases();
  }, [loadUseCases]);

  return (
    <main className="dashboard">
      <header className="dashboard-header">
        <div>
          <p className="eyebrow">Catalog</p>
          <h1>Use Cases</h1>
        </div>
        <button
          className="btn-clear"
          onClick={() => void loadUseCases()}
          disabled={loading}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
      </header>

      {error && <p className="notice error">{error}</p>}
      {loading && useCases.length === 0 && (
        <p className="notice">Loading use cases…</p>
      )}
      {!loading && !error && useCases.length === 0 && (
        <p className="notice">No use cases exist yet.</p>
      )}

      {useCases.length > 0 && (
        <ul className="use-case-list">
          {useCases.map((useCase) => (
            <UseCaseCard key={useCase.id} useCase={useCase} />
          ))}
        </ul>
      )}
    </main>
  );
}

/**
 * One use case and the metrics it is scored with. An empty metric list is not an
 * empty use case: that is `default`, which scores every registered metric and
 * stays in sync as the code catalog grows.
 */
function UseCaseCard({ useCase }: { useCase: UseCase }) {
  const scoresEverything = useCase.metrics.length === 0;

  return (
    <li className="use-case-card">
      <div className="use-case-header">
        <span className="use-case-name">{useCase.name}</span>
        <span className="metric-count">
          {scoresEverything ? "all" : useCase.metrics.length}
        </span>
      </div>
      {scoresEverything ? (
        <p className="use-case-note">Scores every registered metric.</p>
      ) : (
        <div className="use-case-metrics">
          {useCase.metrics.map((metric) => (
            <span key={metric} className="metric-slot-var">
              {metric}
            </span>
          ))}
        </div>
      )}
      <code className="use-case-id">{useCase.id}</code>
    </li>
  );
}
