import React, { useCallback, useEffect, useState } from "react";
import type { UseCase, Metric } from "../types";
import { fetchUseCases, fetchMetrics, updateUseCase } from "../api";

/**
 * Use cases page: lists every use case from GET /api/v1/use-cases with the metric
 * set it scores with. Allows editing the metrics for any use case except `default`.
 */
export function UseCases() {
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [metrics, setMetrics] = useState<Metric[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const loadUseCases = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [useCasesData, metricsData] = await Promise.all([
        fetchUseCases(),
        fetchMetrics(),
      ]);
      setUseCases(useCasesData);
      setMetrics(metricsData);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not load use cases"
      );
    } finally {
      setLoading(false);
    }
  }, []);

  const handleUpdateUseCase = useCallback(
    async (id: string, newMetrics: string[]) => {
      try {
        const updated = await updateUseCase(id, newMetrics);
        setUseCases((prev) =>
          prev.map((uc) => (uc.id === id ? updated : uc))
        );
      } catch (caught) {
        setError(
          caught instanceof Error ? caught.message : "Could not update use case"
        );
      }
    },
    []
  );

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
            <UseCaseCard
              key={useCase.id}
              useCase={useCase}
              availableMetrics={metrics}
              onUpdate={handleUpdateUseCase}
            />
          ))}
        </ul>
      )}
    </main>
  );
}

/**
 * One use case and the metrics it is scored with. An empty metric list is not an
 * empty use case: that is `default`, which scores every registered metric and
 * stays in sync as the code catalog grows. Allows editing metrics except for `default`.
 */
function UseCaseCard({
  useCase,
  availableMetrics,
  onUpdate,
}: {
  useCase: UseCase;
  availableMetrics: Metric[];
  onUpdate: (id: string, metrics: string[]) => Promise<void>;
}) {
  const [isEditing, setIsEditing] = useState(false);
  const [editingMetrics, setEditingMetrics] = useState<Set<string>>(
    new Set(useCase.metrics)
  );
  const [saving, setSaving] = useState(false);
  const [localError, setLocalError] = useState("");

  const scoresEverything = useCase.metrics.length === 0;
  const isDefault = useCase.name === "default";
  const canEdit = !isDefault;

  const availableMetricNames = availableMetrics.map((m) => m.name);
  const selectableMetrics = availableMetricNames.filter(
    (m) => !editingMetrics.has(m)
  );

  const handleToggleMetric = (metric: string, add: boolean) => {
    setEditingMetrics((prev) => {
      const next = new Set(prev);
      if (add) {
        next.add(metric);
      } else {
        next.delete(metric);
      }
      return next;
    });
    setLocalError("");
  };

  const handleSave = async () => {
    if (editingMetrics.size === 0) {
      setLocalError("Al menos una métrica es requerida.");
      return;
    }
    setSaving(true);
    try {
      await onUpdate(useCase.id, Array.from(editingMetrics).sort());
      setIsEditing(false);
    } catch (caught) {
      setLocalError(
        caught instanceof Error ? caught.message : "Error al guardar"
      );
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    setEditingMetrics(new Set(useCase.metrics));
    setIsEditing(false);
    setLocalError("");
  };

  if (isEditing) {
    return (
      <li className="use-case-card editing">
        <div className="use-case-header">
          <span className="use-case-name">{useCase.name}</span>
          <span className="metric-count">{editingMetrics.size}</span>
        </div>

        <p className="notice">
          Los cambios aplican a nuevas evaluaciones y a re-ejecuciones; los
          puntajes existentes se conservan.
        </p>

        <div className="use-case-metrics">
          {Array.from(editingMetrics)
            .sort()
            .map((metric) => (
              <span key={metric} className="metric-slot-var">
                {metric}
                <button
                  className="metric-remove"
                  onClick={() => handleToggleMetric(metric, false)}
                  title="Remover métrica"
                >
                  ×
                </button>
              </span>
            ))}
        </div>

        {selectableMetrics.length > 0 && (
          <div className="metric-picker">
            <label>Agregar métrica:</label>
            <select
              onChange={(e) => {
                if (e.target.value) {
                  handleToggleMetric(e.target.value, true);
                  e.target.value = "";
                }
              }}
              defaultValue=""
            >
              <option value="">Seleccionar...</option>
              {selectableMetrics.map((metric) => (
                <option key={metric} value={metric}>
                  {metric}
                </option>
              ))}
            </select>
          </div>
        )}

        {localError && <p className="notice error">{localError}</p>}

        <div className="use-case-actions">
          <button
            className="btn-clear"
            onClick={handleSave}
            disabled={saving || editingMetrics.size === 0}
          >
            {saving ? "Guardando…" : "Guardar"}
          </button>
          <button
            className="btn-clear"
            onClick={handleCancel}
            disabled={saving}
          >
            Cancelar
          </button>
        </div>

        <code className="use-case-id">{useCase.id}</code>
      </li>
    );
  }

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
      {canEdit && (
        <div className="use-case-actions">
          <button
            className="btn-clear"
            onClick={() => setIsEditing(true)}
          >
            Editar
          </button>
        </div>
      )}
      <code className="use-case-id">{useCase.id}</code>
    </li>
  );
}
