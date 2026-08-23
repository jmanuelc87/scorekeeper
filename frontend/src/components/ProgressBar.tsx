import React from "react";
import type { RunProgress } from "../types";

interface ProgressBarProps {
  progress: RunProgress;
}

/**
 * Renders a horizontal progress bar with percentage label.
 */
export function ProgressBar({ progress }: ProgressBarProps) {
  const percent = Math.round(progress.ratio * 100);

  return (
    <div className="progress-bar-container">
      <div className="progress-bar-track">
        <div
          className="progress-bar-fill"
          style={{ width: `${percent}%` }}
        />
      </div>
      <span className="progress-bar-label">
        {progress.done}/{progress.total} ({percent}%)
      </span>
    </div>
  );
}
