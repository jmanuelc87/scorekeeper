import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Score = {
  id: number;
  player: string;
  points: number;
  created_at: string;
};

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8001";

function App() {
  const [scores, setScores] = useState<Score[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetch(`${API_URL}/api/results`);
      if (!response.ok) throw new Error(`Request failed (${response.status})`);
      setScores(await response.json());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not load scores");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void refresh(); }, []);

  return (
    <main>
      <header>
        <div>
          <p className="eyebrow">Live results</p>
          <h1>Scorekeeper</h1>
        </div>
        <button onClick={() => void refresh()} disabled={loading}>Refresh</button>
      </header>
      {error && <p className="notice error">{error}</p>}
      {loading && <p className="notice">Loading scores…</p>}
      {!loading && !error && scores.length === 0 && (
        <p className="notice">No scores yet. Record one through the API.</p>
      )}
      {scores.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Player</th><th>Points</th><th>Recorded</th></tr></thead>
            <tbody>{scores.map((score) => (
              <tr key={score.id}>
                <td>{score.player}</td>
                <td className="points">{score.points}</td>
                <td>{new Date(score.created_at).toLocaleString()}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
