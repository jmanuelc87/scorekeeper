import React from "react";
import type { PlatformExecution } from "../types";

interface PlatformScoreListProps {
  platforms: PlatformExecution[];
}

/**
 * Renders a compact list of platforms with their scores and scenario counts.
 */
export function PlatformScoreList({ platforms }: PlatformScoreListProps) {
  if (platforms.length === 0) {
    return <span className="no-platforms">No platforms</span>;
  }

  return (
    <ul className="platform-score-list">
      {platforms.map((p) => (
        <li key={p.platform} className="platform-score-item">
          <span className="platform-name">{p.platform}</span>
          <span className="platform-score">
            {p.average_score !== null
              ? (p.average_score * 100).toFixed(1) + "%"
              : "—"}
          </span>
        </li>
      ))}
    </ul>
  );
}
