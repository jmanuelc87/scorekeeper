import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { Layout, type Page } from "./components/Layout";
import { RunsDashboard } from "./components/RunsDashboard";
import { Metrics } from "./components/Metrics";
import { MetricPrompt } from "./components/MetricPrompt";
import { UseCases } from "./components/UseCases";
import { AuthProviders } from "./components/AuthProviders";
import "./styles.css";

function App() {
  const [page, setPage] = useState<Page>("dashboard");
  const [promptId, setPromptId] = useState<string | null>(null);

  return (
    <Layout currentPage={page} onNavigate={setPage}>
      {page === "dashboard" && <RunsDashboard />}
      {page === "metrics" && (
        <Metrics
          onOpenPrompt={(id) => {
            setPromptId(id);
            setPage("metric-prompt");
          }}
        />
      )}
      {page === "metric-prompt" && promptId && (
        <MetricPrompt promptId={promptId} onBack={() => setPage("metrics")} />
      )}
      {page === "use-cases" && <UseCases />}
      {page === "auth-providers" && <AuthProviders />}
    </Layout>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
