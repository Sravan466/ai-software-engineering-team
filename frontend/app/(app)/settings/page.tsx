"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  api,
  LocalStatus,
  ModelProfile,
  ProviderSetting,
  RoleRow,
  RoleSettings,
} from "@/lib/api";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";

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

/** The phases where a model trained on code is worth suggesting. */
const CODE_ROLES = ["backend_engineer", "frontend_engineer", "qa_engineer", "devops_engineer"];

export default function SettingsPage() {
  useChrome({ sub: "Settings" }, []);
  // Two cards share one fact — which models are pulled — and one of them can change
  // it. Held here so downloading a model fills the dropdowns below without a reload,
  // which is most of the point of the download button.
  const [pulled, setPulled] = useState(0);

  return (
    <div className="settings-wrap">
      <h1 style={{ fontSize: "var(--t-2xl)" }}>Settings</h1>
      <p className="prose-lede" style={{ marginTop: 10 }}>
        Run everything locally with Ollama, or add your own cloud keys so the router can reach for a
        stronger model when a phase needs one. Keys are stored on this backend only — they are never
        sent to the browser.
      </p>

      <div style={{ display: "flex", flexDirection: "column", gap: 16, marginTop: 24 }}>
        <LocalModelCard onModelsChanged={() => setPulled((n) => n + 1)} />
        <RoleModelCard refreshKey={pulled} />
        <ApiKeysCard />
      </div>
    </div>
  );
}

// ── Local runtime (Ollama) ───────────────────────────────────────────────────
function LocalModelCard({ onModelsChanged }: { onModelsChanged: () => void }) {
  const [status, setStatus] = useState<LocalStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [pulling, setPulling] = useState<string | null>(null);
  const [pct, setPct] = useState<number | null>(null);
  const [phase, setPhase] = useState("");
  const [wanted, setWanted] = useState("");
  const [selecting, setSelecting] = useState<string | null>(null);
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

  async function pull(model: string) {
    const name = model.trim();
    if (!name) return;
    setPulling(name);
    setError("");
    setPct(null);
    setPhase("Starting…");
    try {
      await api.pullLocalModel(name, (line) => {
        if (line.error) {
          setError(line.error);
          return;
        }
        if (line.status) setPhase(line.status);
        if (line.total && line.completed) setPct(Math.round((line.completed / line.total) * 100));
      });
      await refresh();
      onModelsChanged();
      setPhase("Done");
      setWanted("");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setPulling(null);
    }
  }

  /** Point the local runtime at a model that is already downloaded. */
  async function select(model: string) {
    setSelecting(model);
    setError("");
    try {
      setStatus(await api.setLocalModel(model));
      onModelsChanged();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSelecting(null);
    }
  }

  // No literal here. A model name as a loading placeholder is a claim about what
  // this install runs, made before anyone asked it — and it was wrong for anyone
  // who had chosen a different one.
  const model = status?.default_model;
  const busy = pulling !== null || selecting !== null;

  return (
    <section className="card">
      <div className="sec-head">
        <h2 className="label">Local runtime</h2>
        <span className="rule" />
        <button className="btn btn-sm" onClick={refresh} disabled={loading || busy}>
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

          {status?.reachable && status.has_default && model && (
            <div className="notice">
              <span className="dot dot-ok" style={{ marginTop: 7 }} aria-hidden="true" />
              <div className="notice-body">
                <span className="notice-title">
                  <span className="mono">{model}</span> is ready
                  {status.profile?.supports_schema_format && (
                    <span
                      className="badge badge-ok"
                      style={{ marginLeft: 8, verticalAlign: "middle" }}
                      title={
                        "Each agent's required output shape is sent to the model as a " +
                        "schema, so it cannot answer with anything else."
                      }
                    >
                      Shape-locked
                    </span>
                  )}
                </span>
                <span className="notice-text">
                  Every agent runs on this unless you give one its own model below. Builds set to
                  Local run entirely on this machine, at no cost.
                </span>
              </div>
            </div>
          )}

          {status?.profile && <ModelCapability profile={status.profile} />}

          {status?.reachable && !status.has_default && model && (
            <div className="notice notice-warn">
              {Icon.download}
              <div className="notice-body">
                <span className="notice-title">
                  <span className="mono">{model}</span> hasn&apos;t been downloaded
                </span>
                <span className="notice-text">
                  Ollama is running, but the model this install is set to use isn&apos;t here.
                  Download it, or pick one you already have from the list below. A build won&apos;t
                  start until the model it needs is on this machine.
                </span>
                <div className="notice-actions">
                  <button
                    className="btn btn-sm btn-primary"
                    onClick={() => pull(model)}
                    disabled={busy}
                  >
                    {pulling === model && <span className="btn-spinner" aria-hidden="true" />}
                    {pulling === model ? "Downloading…" : `Download ${model}`}
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* The list used to be a row of badges: proof a model existed, and no way
              to run anything on it. Each row is now the selection itself. */}
          {status?.reachable && (
            <div style={{ marginTop: 18 }}>
              <div className="sec-head" style={{ marginBottom: 10 }}>
                <h3 className="label">Downloaded models</h3>
                <span className="rule" />
                {status.models.length > 0 && (
                  <span className="field-hint">{status.models.length} on this machine</span>
                )}
              </div>

              {status.models.length === 0 ? (
                <p className="field-hint" style={{ marginTop: 0 }}>
                  Nothing downloaded yet. Name one below and it will be pulled here.
                </p>
              ) : (
                <ul className="model-rows">
                  {status.models.map((m) => {
                    const current = m === model;
                    return (
                      <li key={m} className="model-row" data-current={current || undefined}>
                        <span className="model-row-name mono">{m}</span>
                        {status.code_models.includes(m) && (
                          <span
                            className="badge"
                            title="Its name suggests it was trained on code — a guess from the name, not a measurement."
                          >
                            code
                          </span>
                        )}
                        {current ? (
                          <span className="badge badge-ok">
                            <span className="dot dot-ok" aria-hidden="true" />
                            In use
                          </span>
                        ) : (
                          <button
                            className="btn btn-sm"
                            onClick={() => select(m)}
                            disabled={busy}
                            aria-label={`Run agents on ${m} by default`}
                          >
                            {selecting === m && <span className="btn-spinner" aria-hidden="true" />}
                            Use this
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}

              <div className="provider-grid" style={{ marginTop: 14 }}>
                <div className="field" style={{ flex: 1, minWidth: 200 }}>
                  <label htmlFor="pull-model">Download another model</label>
                  <input
                    id="pull-model"
                    className="input input-mono"
                    placeholder="name:tag"
                    value={wanted}
                    disabled={busy}
                    onChange={(e) => setWanted(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") pull(wanted);
                    }}
                  />
                </div>
                <button
                  className="btn btn-primary"
                  onClick={() => pull(wanted)}
                  disabled={busy || !wanted.trim()}
                >
                  {pulling === wanted.trim() && <span className="btn-spinner" aria-hidden="true" />}
                  {Icon.download} Download
                </button>
              </div>
              <p className="field-hint" style={{ marginTop: 8 }}>
                Anything from{" "}
                <a
                  className="link"
                  href="https://ollama.com/library"
                  target="_blank"
                  rel="noreferrer"
                >
                  the Ollama library
                </a>{" "}
                works. It stays on this machine and costs nothing to run.
              </p>

              {pulling && (
                <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 6 }}>
                  <div
                    className="progress"
                    role="progressbar"
                    aria-valuenow={pct ?? undefined}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label={`Downloading ${pulling}`}
                  >
                    <span style={{ width: `${pct ?? 4}%` }} />
                  </div>
                  <span className="field-hint">
                    <span className="mono">{pulling}</span> · {phase}
                    {pct !== null ? ` · ${pct}%` : ""}
                  </span>
                </div>
              )}
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

/**
 * What this model can actually do, asked of the model rather than assumed.
 *
 * The window here is the one the pipeline sends as `num_ctx` — the same resolved
 * number, not a second guess at it. It is shown because it used to be invisible:
 * every call ran at the server default, prompts were truncated from the head where
 * the instructions live, and the only evidence was in a log nobody reads.
 */
function ModelCapability({ profile }: { profile: ModelProfile }) {
  const tokens = (n: number) => `${n.toLocaleString()} tokens`;
  return (
    <div style={{ marginTop: 16 }}>
      <div className="sec-head" style={{ marginBottom: 10 }}>
        <h3 className="label">What it can hold</h3>
        <span className="rule" />
      </div>

      <dl className="facts">
        <div className="fact">
          <dt>Context window</dt>
          <dd className="mono">{tokens(profile.context_window)}</dd>
        </div>
        <div className="fact">
          <dt>Longest reply</dt>
          <dd className="mono">{tokens(profile.max_output_tokens)}</dd>
        </div>
        {profile.parameter_size && (
          <div className="fact">
            <dt>Parameters</dt>
            <dd className="mono">{profile.parameter_size}</dd>
          </div>
        )}
        {profile.quantization && (
          <div className="fact">
            <dt>Quantization</dt>
            <dd className="mono">{profile.quantization}</dd>
          </div>
        )}
      </dl>

      {/* Where the number came from, in one line — because "32,768" means something
          different when the model reported it than when nobody could. */}
      <p className="field-hint" style={{ marginTop: 12 }}>
        {profile.source === "probe" && profile.context_limit
          ? `Read from the model itself; it supports up to ${profile.context_limit.toLocaleString()}.`
          : "This model doesn't report a window, so the configured fallback is in force."}
        {profile.clamp_reason ? ` It is ${profile.clamp_reason}.` : ""}
      </p>

      {profile.warnings.map((warning) => (
        <div className="notice notice-warn" style={{ marginTop: 12 }} key={warning}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-text">{warning}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Which model each agent runs on ───────────────────────────────────────────
/**
 * One row per crew member, plus the three jobs that spend a model call without
 * being one.
 *
 * This is the half that was missing. A model could be downloaded on this very page,
 * progress bar and all, and then never actually run by anything — the only way to
 * *select* one went through an endpoint that rejected the local runtime outright, so
 * every agent carried on running on whatever `.env` said. Each row defaults to "the
 * default model", so an install nobody has touched behaves exactly as it always did.
 */
function RoleModelCard({ refreshKey }: { refreshKey: number }) {
  const [state, setState] = useState<RoleSettings | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [dismissed, setDismissed] = useState(false);

  const refresh = useCallback(async () => {
    setError("");
    try {
      setState(await api.getRoles());
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh, refreshKey]);

  async function choose(role: string, value: string) {
    setSaving(role);
    setError("");
    try {
      setState(await api.setRoleModel(role, value || null));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSaving(null);
    }
  }

  /** A downloaded model whose name says "code", if there is one nothing uses yet.
   *  The list comes from the backend rather than a regex here, so the badge in the
   *  card above and the suggestion in this one cannot disagree about a model. */
  const coder = useMemo(() => {
    const found = state?.code_models?.[0];
    if (!found) return null;
    const used = state.roles.some((r) => CODE_ROLES.includes(r.role) && r.assigned === found);
    return used ? null : found;
  }, [state]);

  async function useCoderForCode(model: string) {
    setSaving("suggestion");
    setError("");
    try {
      let latest: RoleSettings | null = null;
      for (const role of CODE_ROLES) latest = await api.setRoleModel(role, model);
      if (latest) setState(latest);
    } catch (e: any) {
      setError(e.message);
      // Four sequential writes: a failure partway through leaves some roles changed
      // on the server, and leaving the old values on screen would misreport which.
      await refresh();
    } finally {
      setSaving(null);
    }
  }

  const phases = state?.roles.filter((r) => r.kind === "phase") ?? [];
  const support = state?.roles.filter((r) => r.kind === "support") ?? [];
  const chosen = state?.roles.filter((r) => r.assigned).length ?? 0;

  return (
    <section className="card">
      <div className="sec-head">
        <h2 className="label">Which model each agent runs on</h2>
        <span className="rule" />
        {chosen > 0 && (
          <span className="badge">
            {chosen} of {state?.roles.length} given their own
          </span>
        )}
      </div>
      <p className="muted" style={{ margin: "0 0 4px", fontSize: "var(--t-base)", lineHeight: 1.6 }}>
        Leave a row on the default and it runs on{" "}
        <span className="mono">{state?.default_model ?? "…"}</span>. Give one its own model and that
        agent uses it from its next phase — no restart, no file to edit. Only models you have
        actually downloaded appear here.
      </p>

      {state === null ? (
        <div style={{ marginTop: 16 }}>
          <SkeletonLines lines={5} />
        </div>
      ) : (
        <>
          {coder && !dismissed && (
            <div className="notice" style={{ marginTop: 14 }}>
              {Icon.sparkle}
              <div className="notice-body">
                <span className="notice-title">
                  You have <span className="mono">{coder}</span> downloaded
                </span>
                <span className="notice-text">
                  Its name suggests it was trained on code, which is what the four building phases
                  spend their time on. Worth trying — but it is a guess from a name rather than a
                  measurement, so nothing reaches for it on its own.
                </span>
                <div className="notice-actions">
                  <button
                    className="btn btn-sm btn-primary"
                    onClick={() => useCoderForCode(coder)}
                    disabled={saving !== null}
                  >
                    {saving === "suggestion" && <span className="btn-spinner" aria-hidden="true" />}
                    Use it for the building phases
                  </button>
                  <button className="btn btn-sm" onClick={() => setDismissed(true)}>
                    No thanks
                  </button>
                </div>
              </div>
            </div>
          )}

          <ul className="role-rows">
            {phases.map((row) => (
              <RoleLine
                key={row.role}
                row={row}
                state={state}
                busy={saving === row.role}
                disabled={saving !== null}
                onChoose={choose}
              />
            ))}
          </ul>

          <div className="sec-head" style={{ marginTop: 22, marginBottom: 4 }}>
            <h3 className="label">Everything else that costs a call</h3>
            <span className="rule" />
          </div>
          <ul className="role-rows">
            {support.map((row) => (
              <RoleLine
                key={row.role}
                row={row}
                state={state}
                busy={saving === row.role}
                disabled={saving !== null}
                onChoose={choose}
              />
            ))}
          </ul>
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

/** The mark for a support role, which has no crew member of its own to draw. */
const SUPPORT_ICON: Record<string, ReactNode> = {
  debate: Icon.diagram,
  preview: Icon.sparkle,
  embeddings: Icon.api,
};

function RoleLine({
  row,
  state,
  busy,
  disabled,
  onChoose,
}: {
  row: RoleRow;
  state: RoleSettings;
  busy: boolean;
  disabled: boolean;
  onChoose: (role: string, value: string) => void;
}) {
  const agent = AGENT_BY_KEY[row.role];
  const id = `role-${row.role}`;

  // A saved choice the list no longer offers — a model deleted from Ollama since.
  // Kept as an option so the control shows what this role is actually set to,
  // rather than snapping back to a default it is not using.
  const options = useMemo(() => {
    // Embeddings run against Ollama's own endpoint and nothing else, so offering a
    // cloud model here would be offering a choice that cannot be honoured.
    const all =
      row.role === "embeddings"
        ? [...state.local_models]
        : [...state.local_models, ...state.cloud_models];
    return row.assigned && !all.includes(row.assigned) ? [...all, row.assigned] : all;
  }, [state.local_models, state.cloud_models, row.assigned, row.role]);
  const missing =
    Boolean(row.assigned) && row.provider === "ollama" && !state.local_models.includes(row.model ?? "");

  return (
    <li className="role-row" style={{ ["--agent" as string]: agent?.accent }}>
      <span className="role-mark" aria-hidden="true">
        {agent ? <AgentSprite agent={agent} size={26} state="queued" /> : SUPPORT_ICON[row.role]}
      </span>
      <span className="role-id">
        <label htmlFor={id} className="role-name">
          {agent?.codename ?? row.label}
        </label>
        <span className="role-what">{agent ? agent.deliver : row.what}</span>
      </span>
      <span className="role-pick">
        <select
          id={id}
          className="select"
          value={row.assigned ?? ""}
          disabled={disabled}
          onChange={(e) => onChoose(row.role, e.target.value)}
        >
          <option value="">Default — {state.default_model}</option>
          {options.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        {busy && <span className="btn-spinner" aria-hidden="true" />}
      </span>
      {missing && (
        <span className="role-warn" role="status">
          {Icon.alert}
          <span>
            <span className="mono">{row.model}</span> isn&apos;t downloaded — a build using this
            agent won&apos;t start until it is.
          </span>
        </span>
      )}
    </li>
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
