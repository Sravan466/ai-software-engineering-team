"use client";

import { useCallback, useEffect, useState } from "react";
import { api, LocalStatus, ProviderSetting } from "@/lib/api";

const PROVIDERS: { key: string; label: string; placeholder: string }[] = [
  { key: "anthropic", label: "Anthropic — Claude", placeholder: "sk-ant-..." },
  { key: "openai", label: "OpenAI — GPT", placeholder: "sk-..." },
  { key: "gemini", label: "Google — Gemini", placeholder: "AIza..." },
];

export default function SettingsPage() {
  return (
    <div className="space-y-6">
      <div>
        <p className="fd-kicker mb-1">configuration</p>
        <h1 className="fd-title text-2xl">Settings</h1>
        <p className="mt-1 text-sm fd-muted">
          Configure the local model and your own cloud API keys. Keys are stored on this backend
          only (gitignored) and never sent to the browser.
        </p>
      </div>
      <LocalModelCard />
      <ApiKeysCard />
    </div>
  );
}

// ── Local model (Ollama) ─────────────────────────────────────────────────────
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
    setPhase("starting…");
    try {
      await api.pullLocalModel(status.default_model, (line) => {
        if (line.error) {
          setError(line.error);
          return;
        }
        if (line.status) setPhase(line.status);
        if (line.total && line.completed) {
          setPct(Math.round((line.completed / line.total) * 100));
        }
      });
      await refresh();
      setPhase("done");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setPulling(false);
    }
  }

  const model = status?.default_model ?? "qwen2.5:7b";

  return (
    <div className="fd-card">
      <div className="mb-3 flex items-center gap-3">
        <h2 className="fd-kicker">local model</h2>
        <span className="fd-rule" />
        <button className="fd-btn fd-btn-sm" onClick={refresh} disabled={loading || pulling}>
          {loading ? "Checking…" : "Recheck"}
        </button>
      </div>

      {status && (
        <p className="mb-3 text-xs fd-dim">
          Ollama · <span className="fd-mono">{status.base_url}</span>
        </p>
      )}

      {status && !status.reachable && (
        <div className="space-y-2">
          <span className="fd-badge fd-badge-muted">⚠ Ollama not detected</span>
          <p className="text-sm fd-muted">
            Install Ollama and start it, then click Recheck.{" "}
            <a className="fd-link" href="https://ollama.com/download" target="_blank" rel="noreferrer">
              Download Ollama →
            </a>
          </p>
        </div>
      )}

      {status && status.reachable && status.has_default && (
        <span className="fd-badge fd-badge-ok">✓ {model} ready</span>
      )}

      {status && status.reachable && !status.has_default && (
        <div className="space-y-3">
          <span className="fd-badge fd-badge-warn">⬇ {model} not downloaded</span>
          <div>
            <button className="fd-btn fd-btn-primary" onClick={onPull} disabled={pulling}>
              {pulling ? "Downloading…" : `Download ${model} (~4.7 GB)`}
            </button>
          </div>
          {pulling && (
            <div className="space-y-1">
              <div
                className="h-2 w-full overflow-hidden rounded"
                style={{ background: "var(--bg-3)" }}
              >
                <div
                  className="h-full transition-all"
                  style={{ width: `${pct ?? 4}%`, background: "var(--accent)" }}
                />
              </div>
              <p className="text-xs fd-dim">
                {phase}
                {pct !== null ? ` · ${pct}%` : ""}
              </p>
            </div>
          )}
        </div>
      )}

      {status && status.models.length > 0 && (
        <div className="mt-4">
          <p className="fd-kicker mb-1.5">installed models</p>
          <div className="flex flex-wrap gap-2">
            {status.models.map((m) => (
              <span key={m} className="fd-badge fd-badge-muted">
                {m}
              </span>
            ))}
          </div>
        </div>
      )}

      {error && (
        <p className="mt-3 text-sm" style={{ color: "var(--accent)" }}>
          {error}
        </p>
      )}
    </div>
  );
}

// ── Cloud API keys ───────────────────────────────────────────────────────────
function ApiKeysCard() {
  const [providers, setProviders] = useState<Record<string, ProviderSetting> | null>(null);
  const [drafts, setDrafts] = useState<Record<string, { key: string; model: string }>>({});
  const [saving, setSaving] = useState<string | null>(null);
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
    try {
      const draft = drafts[provider] || { key: "", model: "" };
      await api.setProviderKey(provider, {
        api_key: draft.key.trim() ? draft.key.trim() : undefined,
        default_model: draft.model.trim() || undefined,
      });
      setDrafts((d) => ({ ...d, [provider]: { ...d[provider], key: "" } }));
      await refresh();
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
    <div className="fd-card">
      <div className="mb-2 flex items-center gap-3">
        <h2 className="fd-kicker">cloud api keys</h2>
        <span className="fd-rule" />
      </div>
      <p className="mb-5 text-sm fd-muted">
        Add your own keys to use Claude, GPT, or Gemini in <b className="fd-mono">Auto</b> /{" "}
        <b className="fd-mono">Manual</b> routing. Leave blank to stay fully local.
      </p>

      <div className="space-y-5">
        {PROVIDERS.map((p) => {
          const info = providers?.[p.key];
          const draft = drafts[p.key] || { key: "", model: "" };
          return (
            <div
              key={p.key}
              style={{ borderColor: "var(--line)" }}
              className="border-t pt-4 first:border-0 first:pt-0"
            >
              <div className="mb-2 flex items-center justify-between">
                <span className="font-medium">{p.label}</span>
                {info?.configured ? (
                  <span className="fd-badge fd-badge-ok">
                    key set {info.key_hint ? `(${info.key_hint})` : ""}
                  </span>
                ) : (
                  <span className="fd-badge fd-badge-muted">not set</span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  type="password"
                  autoComplete="off"
                  className="fd-input min-w-[220px] flex-1"
                  placeholder={info?.configured ? "Type a new key to replace" : p.placeholder}
                  value={draft.key}
                  onChange={(e) =>
                    setDrafts((d) => ({ ...d, [p.key]: { ...d[p.key], key: e.target.value } }))
                  }
                />
                <input
                  type="text"
                  className="fd-input w-44"
                  placeholder="default model"
                  value={draft.model}
                  onChange={(e) =>
                    setDrafts((d) => ({ ...d, [p.key]: { ...d[p.key], model: e.target.value } }))
                  }
                />
                <button
                  className="fd-btn fd-btn-primary"
                  onClick={() => onSave(p.key)}
                  disabled={saving === p.key}
                >
                  {saving === p.key ? "Saving…" : "Save"}
                </button>
                {info?.configured && (
                  <button
                    className="fd-btn"
                    onClick={() => onRemove(p.key)}
                    disabled={saving === p.key}
                  >
                    Remove
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {error && (
        <p className="mt-3 text-sm" style={{ color: "var(--accent)" }}>
          {error}
        </p>
      )}
    </div>
  );
}
