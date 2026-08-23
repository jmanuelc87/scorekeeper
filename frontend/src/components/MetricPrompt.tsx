import React, { useCallback, useEffect, useState } from "react";
import type { PromptSlotDetail, PromptVersion } from "../types";
import {
  createPromptVersion,
  discardPromptVersion,
  fetchPrompt,
  publishPromptVersion,
} from "../api";

/**
 * Metric prompt page: edit one prompt slot's text and open new versions of it.
 * The text a run scores under is the published, active version — a draft is saved
 * first and published second, which is what validates it against the slot.
 */
export function MetricPrompt({
  promptId,
  onBack,
}: {
  promptId: string;
  onBack: () => void;
}) {
  const [prompt, setPrompt] = useState<PromptSlotDetail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const loadPrompt = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setPrompt(await fetchPrompt(promptId));
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not load the prompt"
      );
    } finally {
      setLoading(false);
    }
  }, [promptId]);

  useEffect(() => {
    void loadPrompt();
  }, [loadPrompt]);

  return (
    <main className="dashboard">
      <header className="dashboard-header">
        <div>
          <p className="eyebrow">Metric Prompt</p>
          <h1>{prompt ? `${prompt.metric} · ${prompt.slug}` : "Prompt"}</h1>
        </div>
        <button onClick={onBack}>← Metrics</button>
      </header>

      {error && <p className="notice error">{error}</p>}
      {loading && !prompt && <p className="notice">Loading prompt…</p>}

      {prompt && (
        <>
          <PromptEditor prompt={prompt} onSaved={() => void loadPrompt()} />
          <section className="metric-category">
            <h3 className="section-title">
              Versions <span className="metric-count">{prompt.versions.length}</span>
            </h3>
            <ul className="metric-version-list">
              {prompt.versions.map((version) => (
                <VersionRow
                  key={version.id}
                  promptId={prompt.id}
                  version={version}
                  onChanged={() => void loadPrompt()}
                />
              ))}
            </ul>
          </section>
        </>
      )}
    </main>
  );
}

/**
 * The editable text plus the metadata of the edit. Saving opens a draft; publishing
 * it is what makes it live, so the two are separate buttons.
 */
function PromptEditor({
  prompt,
  onSaved,
}: {
  prompt: PromptSlotDetail;
  onSaved: () => void;
}) {
  const draft = prompt.versions.find((version) => version.status === "draft") ?? null;
  const active = prompt.versions.find((version) => version.is_active) ?? null;

  const [template, setTemplate] = useState((draft ?? active)?.template ?? "");
  const [changelog, setChangelog] = useState("");
  const [author, setAuthor] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  // A save or publish reloads the slot, so the editor follows the newest text.
  useEffect(() => {
    setTemplate((draft ?? active)?.template ?? "");
  }, [draft?.id, active?.id]);

  const run = async (action: () => Promise<string>) => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const outcome = await action();
      setMessage(outcome);
      onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  const handleSave = () =>
    run(async () => {
      const version = await createPromptVersion(
        prompt.id,
        template,
        changelog.trim() || null,
        author.trim() || null
      );
      setChangelog("");
      return `Draft v${version.version} saved — publish it to make it live`;
    });

  const handlePublish = () =>
    run(async () => {
      if (!draft) throw new Error("Save a draft first");
      const version = await publishPromptVersion(
        prompt.id,
        draft.id,
        author.trim() || null
      );
      return `v${version.version} is now the live version`;
    });

  return (
    <section className="prompt-editor">
      <div className="metric-slot-header">
        <code className="metric-slot-slug">{prompt.slug}</code>
        {prompt.required_variables.map((variable) => (
          <span key={variable} className="metric-slot-var">{`{${variable}}`}</span>
        ))}
      </div>
      {prompt.description && (
        <p className="metric-slot-description">{prompt.description}</p>
      )}

      <textarea
        className="prompt-textarea"
        value={template}
        onChange={(event) => setTemplate(event.target.value)}
        spellCheck={false}
        aria-label="Prompt template"
      />

      <div className="prompt-editor-fields">
        <div className="filter-group">
          <label htmlFor="prompt-changelog">Changelog</label>
          <input
            id="prompt-changelog"
            type="text"
            value={changelog}
            onChange={(event) => setChangelog(event.target.value)}
            placeholder="Why this edit"
          />
        </div>
        <div className="filter-group">
          <label htmlFor="prompt-author">Author</label>
          <input
            id="prompt-author"
            type="text"
            value={author}
            onChange={(event) => setAuthor(event.target.value)}
            placeholder="ana"
          />
        </div>
        <div className="run-card-actions">
          <button
            onClick={() => void handleSave()}
            disabled={busy || template.trim() === ""}
          >
            Save draft
          </button>
          <button
            className="btn-apply"
            onClick={() => void handlePublish()}
            disabled={busy || !draft}
            title={draft ? "" : "Save a draft first"}
          >
            Publish
          </button>
        </div>
      </div>

      <p className="selection-hint">
        {draft
          ? `Editing draft v${draft.version} — it is not scored under until published.`
          : "Saving opens a new draft; the live version is unchanged until you publish."}
      </p>
      {error && <p className="inline-error">{error}</p>}
      {message && <p className="inline-success">{message}</p>}
    </section>
  );
}

/** One version in the history, with the action its status allows. */
function VersionRow({
  promptId,
  version,
  onChanged,
}: {
  promptId: string;
  version: PromptVersion;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const handleDiscard = async () => {
    setBusy(true);
    setError("");
    try {
      await discardPromptVersion(promptId, version.id);
      onChanged();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to discard");
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="metric-version">
      <div className="metric-version-header">
        <span className="metric-version-number">v{version.version}</span>
        <span className={`status-badge status-version-${version.status}`}>
          {version.status}
        </span>
        {version.is_active && (
          <span className="status-badge status-completado">active</span>
        )}
        <span className="metric-meta">
          {version.created_by ?? "—"} ·{" "}
          {new Date(version.created_at).toLocaleString()}
        </span>
        {version.status === "draft" && (
          <button
            className="btn-clear"
            onClick={() => void handleDiscard()}
            disabled={busy}
          >
            Discard
          </button>
        )}
      </div>
      {error && <p className="inline-error">{error}</p>}
    </li>
  );
}
