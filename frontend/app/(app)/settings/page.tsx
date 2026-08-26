"use client";

import { useCallback, useEffect, useState } from "react";
import { api, LocalStatus, ProviderSetting } from "@/lib/api";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";

const PROVIDERS: { key: string; label: string; placeholder: string; console: string }[] = [
  {
    key: "anthropic",
    label: "Anthropic — Claude",
    placeholder: "sk-ant-…",
    console: "https://console.anthropic.com/settings/keys",
  },
  {
    key: "openai",
    label: "OpenAI — GPT",
    placeholder: "sk-…",
    console: "https://platform.openai.com/api-keys",
  },
  {
    key: "gemini",
    label: "Google — Gemini",
    placeholder: "AIza…",
    console: "https://aistudio.google.com/apikey",
  },
];

export default function SettingsPage() {
  useChrome({ sub: "Settings" }, []);
  return (
    <div className="settings-wrap">
      <h1 style={{ fontSize: "var(--t-2xl)" }}>Settings</h1>
      <p className="prose-lede" style={{ marginTop: 10 }}>
        Run everything locally with Ollama, or add your own cloud keys so the router can reach for a
        stronger model when a phase needs one. Keys are stored on this backend only — they are never
        sent to the browser.
      </p>

      <div style={{ display: "flex", flexDirection: "column", gap: 16, marginTop: 24 }}>
        <LocalModelCard />
        <ApiKeysCard />
      </div>
    </div>
  );
}

// ── Local runtime (Ollama) ───────────────────────────────────────────────────
function LocalModelCard() {
  const [status, setStatus] = useState<LocalStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [pulling, setPulling] = useState(false);
  const [pct, setPct] = useState<number | null>(null);
  const [phase, setPhase] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setStatus(await api.getLocalModel());
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function onPull() {
    if (!status) return;
    setPulling(true);
    setError("");
    setPct(null);
    setPhase("Starting…");
    try {
      await api.pullLocalModel(status.default_model, (line) => {
        if (line.error) {
          setError(line.error);
          return;
        }
        if (line.status) setPhase(line.status);
        if (line.total && line.completed) setPct(Math.round((line.completed / line.total) * 100));
      });
      await refresh();
      setPhase("Done");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setPulling(false);
    }
  }

  const model = status?.default_model ?? "qwen2.5:7b";

  return (
    <section className="card">
      <div className="sec-head">
        <h2 className="label">Local runtime</h2>
        <span className="rule" />
        <button className="btn btn-sm" onClick={refresh} disabled={loading || pulling}>
          {loading ? <span className="btn-spinner" aria-hidden="true" /> : Icon.refresh}
          {loading ? "Checking…" : "Recheck"}
        </button>
      </div>

      {loading && !status ? (
        <SkeletonLines lines={2} />
      ) : (
        <>
          {status && (
            <p className="field-hint" style={{ marginTop: 0, marginBottom: 14 }}>
              Ollama at <span className="mono">{status.base_url}</span>
            </p>
          )}

          {/* Nothing in this product works without a local model, so a missing
              one is a notice with the fix attached — not a grey badge. */}
          {status && !status.reachable && (
            <div className="notice notice-warn">
              {Icon.alert}
              <div className="notice-body">
                <span className="notice-title">Ollama isn&apos;t running</span>
                <span className="notice-text">
                  Builds in Local mode need Ollama on this machine. Install it, start it, then
                  recheck — everything else here is already configured.
                </span>
                <div className="notice-actions">
                  <a
                    className="btn btn-sm btn-primary"
                    href="https://ollama.com/download"
                    target="_blank"
                    rel="noreferrer"
                  >
                    Download Ollama {Icon.external}
                  </a>
                  <button className="btn btn-sm" onClick={refresh} disabled={loading}>
                    Recheck
                  </button>
                </div>
              </div>
            </div>
          )}

          {status?.reachable && status.has_default && (
            <div className="notice">
              <span className="dot dot-ok" style={{ marginTop: 7 }} aria-hidden="true" />
              <div className="notice-body">
                <span className="notice-title">
                  <span className="mono">{model}</span> is ready
                </span>
                <span className="notice-text">
                  Builds set to Local will run entirely on this machine, at no cost.
                </span>
              </div>
            </div>
          )}

          {status?.reachable && !status.has_default && (
            <div className="notice notice-warn">
              {Icon.download}
              <div className="notice-body">
                <span className="notice-title">
                  <span className="mono">{model}</span> hasn&apos;t been downloaded
                </span>
                <span className="notice-text">
                  Ollama is running but the default model is missing. It&apos;s about 4.7&nbsp;GB and
                  only needs downloading once.
                </span>
                <div className="notice-actions">
                  <button className="btn btn-sm btn-primary" onClick={onPull} disabled={pulling}>
                    {pulling && <span className="btn-spinner" aria-hidden="true" />}
                    {pulling ? "Downloading…" : `Download ${model}`}
                  </button>
                </div>
                {pulling && (
                  <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 6 }}>
                    <div
                      className="progress"
                      role="progressbar"
                      aria-valuenow={pct ?? undefined}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-label={`Downloading ${model}`}
                    >
                      <span style={{ width: `${pct ?? 4}%` }} />
                    </div>
                    <span className="field-hint">
                      {phase}
                      {pct !== null ? ` · ${pct}%` : ""}
                    </span>
                  </div>
                )}
              </div>
            </div>
          )}

          {status && status.models.length > 0 && (
            <div style={{ marginTop: 18 }}>
              <h3 className="label" style={{ marginBottom: 8 }}>
                Installed models
              </h3>
              <div className="model-list">
                {status.models.map((m) => (
                  <span key={m} className="badge badge-mono">
                    {m}
                  </span>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {error && (
        <div className="notice notice-bad" role="alert" style={{ marginTop: 14 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-text">{error}</span>
          </div>
        </div>
      )}
    </section>
  );
}

// ── Cloud API keys ───────────────────────────────────────────────────────────
function ApiKeysCard() {
  const [providers, setProviders] = useState<Record<string, ProviderSetting> | null>(null);
  const [drafts, setDrafts] = useState<Record<string, { key: string; model: string }>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setError("");
    try {
      const { providers } = await api.getProviders();
      setProviders(providers);
      setDrafts((d) => {
        const next = { ...d };
        for (const p of PROVIDERS) {
          next[p.key] = {
            key: "",
            model: next[p.key]?.model ?? providers[p.key]?.default_model ?? "",
          };
        }
        return next;
      });
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function onSave(provider: string) {
    setSaving(provider);
    setError("");
    setSaved(null);
    try {
      const draft = drafts[provider] || { key: "", model: "" };
      await api.setProviderKey(provider, {
        api_key: draft.key.trim() ? draft.key.trim() : undefined,
        default_model: draft.model.trim() || undefined,
      });
      setDrafts((d) => ({ ...d, [provider]: { ...d[provider], key: "" } }));
      await refresh();
      // Confirm the write, then let the confirmation fade on its own.
      setSaved(provider);
      setTimeout(() => setSaved((s) => (s === provider ? null : s)), 2600);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSaving(null);
    }
  }

  async function onRemove(provider: string) {
    setSaving(provider);
    setError("");
    try {
      await api.setProviderKey(provider, { api_key: "" });
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSaving(null);
    }
  }

  return (
    <section className="card">
      <div className="sec-head">
        <h2 className="label">Cloud API keys</h2>
        <span className="rule" />
      </div>
      <p className="muted" style={{ margin: "0 0 6px", fontSize: "var(--t-base)", lineHeight: 1.6 }}>
        Add your own keys to let Auto and Manual routing reach Claude, GPT or Gemini. Leave them
        blank to stay entirely local.
      </p>

      {providers === null ? (
        <div style={{ marginTop: 16 }}>
          <SkeletonLines lines={4} />
        </div>
      ) : (
        PROVIDERS.map((p) => {
          const info = providers[p.key];
          const draft = drafts[p.key] || { key: "", model: "" };
          const isSaving = saving === p.key;
          return (
            <div key={p.key} className="provider">
              <div className="provider-head">
                <span className="provider-name">{p.label}</span>
                {info?.configured ? (
                  <span className="badge badge-ok">
                    <span className="dot dot-ok" aria-hidden="true" />
                    Key saved{info.key_hint ? ` · ${info.key_hint}` : ""}
                  </span>
                ) : (
                  <span className="badge">Not configured</span>
                )}
              </div>

              <div className="provider-grid">
                <div className="field field-key">
                  <label htmlFor={`key-${p.key}`}>API key</label>
                  <input
                    id={`key-${p.key}`}
                    type="password"
                    autoComplete="off"
                    className="input input-mono"
                    placeholder={info?.configured ? "Enter a new key to replace it" : p.placeholder}
                    value={draft.key}
                    disabled={isSaving}
                    onChange={(e) =>
                      setDrafts((d) => ({ ...d, [p.key]: { ...d[p.key], key: e.target.value } }))
                    }
                  />
                </div>
                <div className="field field-model">
                  <label htmlFor={`model-${p.key}`}>Default model</label>
                  <input
                    id={`model-${p.key}`}
                    type="text"
                    className="input input-mono"
                    placeholder="model id"
                    value={draft.model}
                    disabled={isSaving}
                    onChange={(e) =>
                      setDrafts((d) => ({ ...d, [p.key]: { ...d[p.key], model: e.target.value } }))
                    }
                  />
                </div>
                <button className="btn btn-primary" onClick={() => onSave(p.key)} disabled={isSaving}>
                  {isSaving && <span className="btn-spinner" aria-hidden="true" />}
                  {isSaving ? "Saving…" : "Save"}
                </button>
                {info?.configured && (
                  <button className="btn btn-danger" onClick={() => onRemove(p.key)} disabled={isSaving}>
                    Remove
                  </button>
                )}
              </div>

              <p className="field-hint" style={{ marginTop: 8 }} aria-live="polite">
                {saved === p.key ? (
                  <span style={{ color: "var(--ok)" }}>Saved.</span>
                ) : (
                  <>
                    Get a key from{" "}
                    <a className="link" href={p.console} target="_blank" rel="noreferrer">
                      {p.label.split(" — ")[0]}
                    </a>
                    .
                  </>
                )}
              </p>
            </div>
          );
        })
      )}

      {error && (
        <div className="notice notice-bad" role="alert" style={{ marginTop: 14 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-text">{error}</span>
          </div>
        </div>
      )}
    </section>
  );
}
