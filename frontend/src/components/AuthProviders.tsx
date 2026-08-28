import React, { useCallback, useEffect, useState } from "react";
import type { AuthProvider, AuthProviderInput } from "../types";
import {
  createAuthProvider,
  deleteAuthProvider,
  fetchAuthProviders,
  updateAuthProvider,
} from "../api";

/**
 * The kinds the credential-provider registry implements. The API has no endpoint
 * that lists them, so the picker mirrors `core/retrieval/credentials/catalog/`;
 * a kind with no implementation is rejected with a 422 naming the known ones.
 */
const PROVIDER_KINDS = ["sharepoint", "oauth2"];

/** Editable shape of a provider row — every field a string, as the inputs hold it. */
interface FormState {
  provider: string;
  host: string;
  enabled: boolean;
  tenant_id: string;
  client_id: string;
  thumbprint: string;
  site_url: string;
  settings: string;
  private_key: string;
}

const EMPTY_FORM: FormState = {
  provider: PROVIDER_KINDS[0],
  host: "",
  enabled: true,
  tenant_id: "",
  client_id: "",
  thumbprint: "",
  site_url: "",
  settings: "",
  private_key: "",
};

/**
 * Auth Providers page: the retrieval pipeline's credential store, one row per
 * gated host. Certificate private keys are write-only — a row only reports
 * whether one is stored, so the field is always blank and rotates the key when
 * filled in.
 */
export function AuthProviders() {
  const [providers, setProviders] = useState<AuthProvider[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  /** The row being edited, "new" while composing one, or null when the form is closed. */
  const [editing, setEditing] = useState<AuthProvider | "new" | null>(null);

  const loadProviders = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setProviders(await fetchAuthProviders());
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not load auth providers"
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProviders();
  }, [loadProviders]);

  const handleDelete = async (provider: AuthProvider) => {
    if (!window.confirm(`Delete the ${provider.provider} provider for ${provider.host}?`)) {
      return;
    }
    try {
      await deleteAuthProvider(provider.id);
      await loadProviders();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not delete the provider");
    }
  };

  return (
    <main className="dashboard">
      <header className="dashboard-header">
        <div>
          <p className="eyebrow">Retrieval</p>
          <h1>Auth Providers</h1>
        </div>
        <div className="run-card-actions">
          <button
            className="btn-clear"
            onClick={() => void loadProviders()}
            disabled={loading}
          >
            {loading ? "Loading…" : "Refresh"}
          </button>
          <button className="btn-apply" onClick={() => setEditing("new")}>
            New provider
          </button>
        </div>
      </header>

      {error && <p className="notice error">{error}</p>}

      {editing && (
        <ProviderForm
          key={editing === "new" ? "new" : editing.id}
          provider={editing === "new" ? null : editing}
          onCancel={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            void loadProviders();
          }}
        />
      )}

      {loading && providers.length === 0 && (
        <p className="notice">Loading auth providers…</p>
      )}
      {!loading && !error && providers.length === 0 && (
        <p className="notice">No credential providers are configured yet.</p>
      )}

      {providers.length > 0 && (
        <ul className="auth-provider-list">
          {providers.map((provider) => (
            <ProviderCard
              key={provider.id}
              provider={provider}
              onEdit={() => setEditing(provider)}
              onDelete={() => void handleDelete(provider)}
            />
          ))}
        </ul>
      )}
    </main>
  );
}

/** One configured provider: the host it authorizes and the settings it carries. */
function ProviderCard({
  provider,
  onEdit,
  onDelete,
}: {
  provider: AuthProvider;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const fields: [string, string | null][] = [
    ["Tenant", provider.tenant_id],
    ["Client", provider.client_id],
    ["Thumbprint", provider.thumbprint],
    ["Site URL", provider.site_url],
  ];

  return (
    <li className="auth-provider-card">
      <div className="auth-provider-header">
        <span className="auth-provider-kind">{provider.provider}</span>
        <span
          className={`status-badge ${provider.enabled ? "status-completado" : "status-version-discarded"}`}
        >
          {provider.enabled ? "enabled" : "disabled"}
        </span>
      </div>
      <p className="auth-provider-host">{provider.host}</p>

      <dl className="auth-provider-fields">
        {fields.map(([label, value]) => (
          <div key={label} className="auth-provider-field">
            <dt>{label}</dt>
            <dd>{value || "—"}</dd>
          </div>
        ))}
        <div className="auth-provider-field">
          <dt>Private key</dt>
          <dd>{provider.has_private_key ? "stored (encrypted)" : "—"}</dd>
        </div>
      </dl>

      {provider.settings && Object.keys(provider.settings).length > 0 && (
        <pre className="auth-provider-settings">
          {JSON.stringify(provider.settings, null, 2)}
        </pre>
      )}

      <div className="run-card-actions">
        <button className="btn-clear" onClick={onEdit}>
          Edit
        </button>
        <button className="btn-delete" onClick={onDelete}>
          Delete
        </button>
      </div>
      <code className="use-case-id">{provider.id}</code>
    </li>
  );
}

/**
 * Create or edit one provider row. Every field is sent on save, except a blank
 * private key, which is omitted so the stored one is kept rather than rotated.
 */
function ProviderForm({
  provider,
  onCancel,
  onSaved,
}: {
  provider: AuthProvider | null;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<FormState>(() => toForm(provider));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const set = (field: keyof FormState, value: string | boolean) =>
    setForm((current) => ({ ...current, [field]: value }));

  const handleSave = async () => {
    setSaving(true);
    setError("");
    try {
      const body = toBody(form);
      if (provider) {
        await updateAuthProvider(provider.id, body);
      } else {
        await createAuthProvider(body);
      }
      onSaved();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save the provider");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="auth-provider-form">
      <h3 className="section-title">
        {provider ? `Edit ${provider.provider} · ${provider.host}` : "New provider"}
      </h3>

      <div className="auth-provider-form-fields">
        <div className="filter-group">
          <label htmlFor="auth-provider-kind">Provider</label>
          <select
            id="auth-provider-kind"
            value={form.provider}
            onChange={(event) => set("provider", event.target.value)}
          >
            {PROVIDER_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {kind}
              </option>
            ))}
          </select>
        </div>

        <TextField
          id="auth-provider-host"
          label="Host"
          placeholder="contoso.sharepoint.com"
          value={form.host}
          onChange={(value) => set("host", value)}
        />
        <TextField
          id="auth-provider-tenant"
          label="Tenant ID"
          value={form.tenant_id}
          onChange={(value) => set("tenant_id", value)}
        />
        <TextField
          id="auth-provider-client"
          label="Client ID"
          value={form.client_id}
          onChange={(value) => set("client_id", value)}
        />
        <TextField
          id="auth-provider-thumbprint"
          label="Thumbprint"
          value={form.thumbprint}
          onChange={(value) => set("thumbprint", value)}
        />
        <TextField
          id="auth-provider-site"
          label="Site URL"
          value={form.site_url}
          onChange={(value) => set("site_url", value)}
        />

        <label className="auth-provider-toggle">
          <input
            type="checkbox"
            className="turn-checkbox"
            checked={form.enabled}
            onChange={(event) => set("enabled", event.target.checked)}
          />
          Enabled
        </label>
      </div>

      <div className="filter-group auth-provider-wide">
        <label htmlFor="auth-provider-settings">Settings (JSON)</label>
        <textarea
          id="auth-provider-settings"
          className="prompt-textarea auth-provider-textarea"
          value={form.settings}
          placeholder={'{\n  "token_url": "https://…/oauth2/v2.0/token"\n}'}
          onChange={(event) => set("settings", event.target.value)}
        />
      </div>

      <div className="filter-group auth-provider-wide">
        <label htmlFor="auth-provider-key">Private key (PEM)</label>
        <textarea
          id="auth-provider-key"
          className="prompt-textarea auth-provider-textarea"
          value={form.private_key}
          placeholder={
            provider?.has_private_key
              ? "A key is stored — paste a new one to rotate it, or leave blank to keep it."
              : "-----BEGIN PRIVATE KEY-----"
          }
          onChange={(event) => set("private_key", event.target.value)}
        />
        <p className="auth-provider-hint">
          Stored encrypted and never read back.
        </p>
      </div>

      {error && <p className="inline-error">{error}</p>}

      <div className="run-card-actions auth-provider-form-actions">
        <button className="btn-clear" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
        <button
          className="btn-apply"
          onClick={() => void handleSave()}
          disabled={saving || form.host.trim() === ""}
        >
          {saving ? "Saving…" : provider ? "Save changes" : "Create provider"}
        </button>
      </div>
    </section>
  );
}

/** A labelled single-line field, the shape every provider column but `settings` has. */
function TextField({
  id,
  label,
  value,
  placeholder,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="filter-group">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
  );
}

/** Fill the form from a row, or with the defaults for a new one. */
function toForm(provider: AuthProvider | null): FormState {
  if (!provider) return EMPTY_FORM;
  return {
    provider: provider.provider,
    host: provider.host,
    enabled: provider.enabled,
    tenant_id: provider.tenant_id ?? "",
    client_id: provider.client_id ?? "",
    thumbprint: provider.thumbprint ?? "",
    site_url: provider.site_url ?? "",
    settings: provider.settings ? JSON.stringify(provider.settings, null, 2) : "",
    private_key: "",
  };
}

/**
 * Project the form onto the request body: blank optional fields become null, and a
 * blank private key is omitted so it is never rotated by accident.
 */
function toBody(form: FormState): AuthProviderInput {
  const body: AuthProviderInput = {
    provider: form.provider,
    host: form.host.trim(),
    enabled: form.enabled,
    tenant_id: nullable(form.tenant_id),
    client_id: nullable(form.client_id),
    thumbprint: nullable(form.thumbprint),
    site_url: nullable(form.site_url),
    settings: parseSettings(form.settings),
  };
  if (form.private_key.trim() !== "") {
    body.private_key = form.private_key;
  }
  return body;
}

/** A blank input means "no value", which the API stores as null. */
function nullable(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

/** Parse the settings textarea, rejecting anything that is not a JSON object. */
function parseSettings(text: string): Record<string, unknown> | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error("Settings must be valid JSON");
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Settings must be a JSON object");
  }
  return parsed as Record<string, unknown>;
}
