import React from "react";
import type { RunStatus } from "../types";

interface StatusBadgeProps {
  status: RunStatus | string;
}

const STATUS_LABELS: Record<string, string> = {
  ingerido: "Ingested",
  en_cola: "Queued",
  en_proceso: "In Progress",
  completado: "Completed",
  parcial: "Partial",
  fallido: "Failed",
};

/**
 * Renders a colored badge for a run/scenario status.
 */
export function StatusBadge({ status }: StatusBadgeProps) {
  const label = STATUS_LABELS[status] ?? status;
  return <span className={`status-badge status-${status}`}>{label}</span>;
}
