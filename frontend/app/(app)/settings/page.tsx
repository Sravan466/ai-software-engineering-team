"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import {
  api,
  LocalSource,
  LocalStatus,
  ModelCheck,
  ModelProfile,
  ProviderSetting,
  RoleRow,
  RoleSettings,
  SourceModel,
  UnknownEndpoint,
} from "@/lib/api";
import { canRunABuild, runtimeSays } from "@/lib/capabilities";
import { hostOf, modelFor, modelName, sourceFor, triedText } from "@/lib/models";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { CheckDetail, VerdictChip } from "@/components/models/ModelCheck";
import ModelTune from "@/components/models/ModelTune";

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
  // Two cards share one fact — which models the sources serve — and one of them can
  // change it. Held here so adding or downloading a model fills the dropdowns below
  // without a reload, which is most of the point of adding one.
  const [changed, setChanged] = useState(0);

  return (
    <div className="settings-wrap">
      <h1 style={{ fontSize: "var(--t-2xl)" }}>Settings</h1>
      <p className="prose-lede" style={{ marginTop: 10 }}>
        Run everything on models you host — on any local runtime — or add your own cloud keys
        so the router can reach for a stronger model when a phase needs one. Keys are stored on
        this backend only; they are never sent to the browser.
      </p>

      <div style={{ display: "flex", flexDirection: "column", gap: 16, marginTop: 24 }}>
        <LocalSourcesCard onModelsChanged={() => setChanged((n) => n + 1)} />
        <RoleModelCard refreshKey={changed} />
        <ApiKeysCard />
      </div>
    </div>
  );
}

/**
 * Whether an address is this machine: a loopback IP literal, or `localhost`.
 *
 * A name is never read by its spelling — `127.x.10.0.0.5.nip.io` starts like
 * loopback and resolves to another computer. The server applies the same rule and
 * is the authority; this only decides whether to ask before it does.
 */
function isLoopback(address: string): boolean {
  let host: string;
  try {
    host = new URL(address.includes("://") ? address : `http://${address}`).hostname;
  } catch {
    return true; // not an address yet; the server says what is wrong with it
  }
  host = host.replace(/^\[|\]$/g, "").replace(/\.$/, "").toLowerCase();
  // `0.0.0.0` is where a runtime says it listens, and connecting to it is this machine.
  if (host === "localhost" || host === "::1" || host === "::" || host === "0.0.0.0") return true;
  // The URL parser writes an IPv4-mapped loopback as hex: `::ffff:7f00:1`.
  if (/^::ffff:7f[0-9a-f]{2}:[0-9a-f]{1,4}$/.test(host)) return true;
  if (host.startsWith("::ffff:")) host = host.slice("::ffff:".length);
  const v4 = host.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  return Boolean(v4 && v4.slice(1).every((n) => Number(n) <= 255) && (v4[1] === "127" || host === "0.0.0.0"));
}

const ORIGIN_LABEL: Record<LocalSource["origin"], string> = {
  detected: "Found on this machine",
  configured: "From configuration",
  added: "Added here",
};

const DEFAULT_ORIGIN: Record<string, string> = {
  chosen: "You chose it.",
  configured: "Named in this backend's configuration.",
  detected: "Picked automatically — the first model found that can write. Choose one below to keep it.",
};

// ── Local model sources ──────────────────────────────────────────────────────
/**
 * Every runtime this backend can reach, and every model each one serves.
 *
 * A source is a runtime found on this machine — loopback only, never the network —
 * one named in `.env`, or one added here. None of them is special: the default is
 * whichever model you choose, on whichever source serves it.
 */
function LocalSourcesCard({ onModelsChanged }: { onModelsChanged: () => void }) {
  const [status, setStatus] = useState<LocalStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [selecting, setSelecting] = useState<string | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async (probe = false) => {
    setLoading(true);
    setError("");
    try {
      setStatus(await api.getLocalModel(probe));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Whether each model will run a build here. Asked after the list, never before
  // it: describing every model can take a moment the first time, and the list
  // should not wait on it. A failed ask leaves the verdicts out, not the models.
  const [checks, setChecks] = useState<Record<string, ModelCheck> | null>(null);
  useEffect(() => {
    if (!status?.reachable) return;
    let live = true;
    api
      .getCompatibility()
      .then((next) => live && setChecks(next.checks))
      .catch(() => live && setChecks({}));
    return () => {
      live = false;
    };
  }, [status]);
  const onCheck = useCallback(
    (spec: string, check: ModelCheck) => setChecks((c) => ({ ...(c ?? {}), [spec]: check })),
    [],
  );

  /** Make a model every agent's default. */
  async function select(spec: string) {
    setSelecting(spec);
    setError("");
    try {
      setStatus(await api.setLocalModel(spec));
      onModelsChanged();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSelecting(null);
    }
  }

  function changed(next: LocalStatus) {
    setStatus(next);
    setError("");
    onModelsChanged();
  }

  const defaultSpec = status?.default_model ?? null;
  const home = sourceFor(status, defaultSpec);
  const name = modelName(defaultSpec);
  const writes = defaultSpec ? canRunABuild(defaultSpec, status?.cannot_build) : false;

  return (
    <section className="card" aria-labelledby="sources-title">
      <div className="sec-head">
        <h2 className="label" id="sources-title">
          Local model sources
        </h2>
        <span className="rule" />
        <button
          className="btn btn-sm"
          onClick={() => refresh(true)}
          disabled={loading || selecting !== null}
          title="Look for runtimes on this machine again"
        >
          {loading ? <span className="btn-spinner" aria-hidden="true" /> : Icon.refresh}
          {loading ? "Checking…" : "Rescan"}
        </button>
      </div>
      <p className="muted" style={{ margin: "0 0 14px", fontSize: "var(--t-base)", lineHeight: 1.6 }}>
        Runtimes on this machine are found by themselves — on loopback only, never beyond this
        computer. Every model each one serves is listed; choose the one every agent falls back to.
      </p>

      {loading && !status ? (
        <SkeletonLines lines={3} />
      ) : status ? (
        <>
          <DefaultNotice
            status={status}
            home={home}
            name={name}
            writes={writes}
            onDownloaded={() => {
              refresh();
              onModelsChanged();
            }}
          />

          {status.profile && defaultSpec && writes && <ModelCapability profile={status.profile} />}

          <ul className="source-list" aria-label="Sources">
            {status.sources.map((source) => (
              <SourceBlock
                key={source.id}
                source={source}
                status={status}
                checks={checks}
                onCheck={onCheck}
                selecting={selecting}
                onSelect={select}
                onChanged={changed}
                onRefresh={() => {
                  refresh();
                  onModelsChanged();
                }}
                onError={setError}
              />
            ))}
          </ul>

          <UnknownEndpoints unknown={status.unknown} onChanged={changed} onError={setError} />
          <AddSource onChanged={changed} />
        </>
      ) : null}

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

/** The one sentence about the default — the model a build without choices runs on. */
function DefaultNotice({
  status,
  home,
  name,
  writes,
  onDownloaded,
}: {
  status: LocalStatus;
  home: LocalSource | null;
  name: string;
  writes: boolean;
  onDownloaded: () => void;
}) {
  const spec = status.default_model;

  // Nothing in this product builds without a model, so a missing one is a notice
  // with the way out attached — not a grey badge.
  if (!status.reachable) {
    const tried = triedText(status.tried);
    return (
      <div className="notice notice-warn">
        {Icon.alert}
        <div className="notice-body">
          <span className="notice-title">No local runtime reachable</span>
          <span className="notice-text">
            {tried ? <>Nothing answered at <span className="mono">{tried}</span>. </> : null}
            Builds in Local mode need a model runtime serving at least one model. Start one —
            any server that speaks the OpenAI API works — then rescan, or add its address below.
          </span>
        </div>
      </div>
    );
  }
  if (!spec) {
    return (
      <div className="notice notice-warn">
        {Icon.alert}
        <div className="notice-body">
          <span className="notice-title">No local model can write</span>
          <span className="notice-text">
            The runtimes below are answering, but none serves a model that completes text — and
            every agent has to write. Add one to a runtime, then rescan.
          </span>
        </div>
      </div>
    );
  }
  if (home && !home.reachable) {
    return (
      <div className="notice notice-warn">
        {Icon.alert}
        <div className="notice-body">
          <span className="notice-title">
            {home.label} isn&apos;t answering
          </span>
          <span className="notice-text">
            The default, <span className="mono">{name}</span>, is on {home.label} at{" "}
            <span className="mono">{hostOf(home.base_url)}</span>. Start it again, or choose a model
            from a runtime that is running.
          </span>
        </div>
      </div>
    );
  }
  if (!status.has_default) {
    return (
      <div className="notice notice-warn">
        {Icon.download}
        <div className="notice-body">
          <span className="notice-title">
            <span className="mono">{name}</span> isn&apos;t on {home?.label ?? "its runtime"}
          </span>
          <span className="notice-text">
            The default model isn&apos;t served there. {home?.can_download ? "Download it, or pick" : "Pick"}{" "}
            one it does serve from the list below. A build won&apos;t start until the model it needs is
            available.
          </span>
          {home?.can_download && (
            <div className="notice-actions">
              <DownloadButton source={home} model={name} onDone={onDownloaded} primary />
            </div>
          )}
        </div>
      </div>
    );
  }
  if (modelFor(status, spec)?.is_local === false) {
    return (
      <div className="notice notice-warn" role="status">
        {Icon.alert}
        <div className="notice-body">
          <span className="notice-title">
            <span className="mono">{name}</span> runs on a hosted service
          </span>
          <span className="notice-text">
            {home?.label ?? "Its runtime"} sends this model elsewhere to run, so Local builds refuse
            it. Choose a model that runs on your own hardware as the default.
          </span>
        </div>
      </div>
    );
  }
  if (!writes) {
    return (
      <div className="notice notice-warn" role="status">
        {Icon.alert}
        <div className="notice-body">
          <span className="notice-title">
            <span className="mono">{name}</span> can&apos;t run a build
          </span>
          <span className="notice-text">
            The runtime {runtimeSays(status.model_capabilities?.[spec] ?? [])} — it can&apos;t write,
            and every agent has to. Builds refuse to start on it, however each agent below is set.
            Choose a model that writes.
          </span>
        </div>
      </div>
    );
  }
  return (
    <div className="notice">
      <span className="dot dot-ok" style={{ marginTop: 7 }} aria-hidden="true" />
      <div className="notice-body">
        <span className="notice-title">
          <span className="mono">{name}</span> on {home?.label ?? "its runtime"} is the default
          {status.profile?.supports_schema_format && (
            <span
              className="badge badge-ok"
              style={{ marginLeft: 8, verticalAlign: "middle" }}
              title={
                "Each agent's required output shape is sent to the model as a schema, so it " +
                "cannot answer with anything else."
              }
            >
              Shape-locked
            </span>
          )}
        </span>
        <span className="notice-text">
          Every agent runs on this unless you give one its own model below. Builds set to Local
          run on your own hardware, at no cost.{" "}
          {status.default_origin ? DEFAULT_ORIGIN[status.default_origin] : ""}
        </span>
      </div>
    </div>
  );
}

/** One source: whether it answers, where it is, and every model it serves. */
function SourceBlock({
  source,
  status,
  checks,
  onCheck,
  selecting,
  onSelect,
  onChanged,
  onRefresh,
  onError,
}: {
  source: LocalSource;
  status: LocalStatus;
  checks: Record<string, ModelCheck> | null;
  onCheck: (spec: string, check: ModelCheck) => void;
  selecting: string | null;
  onSelect: (spec: string) => void;
  onChanged: (next: LocalStatus) => void;
  onRefresh: () => void;
  onError: (message: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [editingKey, setEditingKey] = useState(false);
  const [key, setKey] = useState("");
  const [savingKey, setSavingKey] = useState(false);
  const headingId = `source-heading-${source.id}`;
  // Where focus goes when a small inline form closes: back to what opened it, and
  // onto the safe choice ("Keep") when a removal asks to be confirmed.
  const keyButton = useRef<HTMLButtonElement>(null);
  const removeButton = useRef<HTMLButtonElement>(null);
  const keepButton = useRef<HTMLButtonElement>(null);
  const focusNext = useRef<"key" | "remove" | "keep" | null>(null);
  useEffect(() => {
    const target = focusNext.current;
    focusNext.current = null;
    if (target === "key") keyButton.current?.focus();
    if (target === "remove") removeButton.current?.focus();
    if (target === "keep") keepButton.current?.focus();
  }, [editingKey, confirming]);

  async function remove() {
    setRemoving(true);
    try {
      onChanged(await api.removeSource(source.id));
    } catch (e: any) {
      onError(e.message);
    } finally {
      setRemoving(false);
      setConfirming(false);
    }
  }

  async function saveKey(value: string) {
    setSavingKey(true);
    try {
      onChanged(await api.setSourceKey(source.id, value));
      setKey("");
      focusNext.current = "key";
      setEditingKey(false);
    } catch (e: any) {
      onError(e.message);
    } finally {
      setSavingKey(false);
    }
  }

  const runtime =
    source.runtime_label && source.runtime_label !== source.label ? source.runtime_label : null;

  return (
    <li className="source" data-down={source.reachable ? undefined : true} aria-labelledby={headingId}>
      <div className="source-head">
        <span className={"dot " + (source.reachable ? "dot-ok" : "dot-warn")} aria-hidden="true" />
        <h3 className="source-name" id={headingId}>
          {source.label}
        </h3>
        {runtime && <span className="source-runtime">{runtime}</span>}
        <span className={"source-state" + (source.reachable ? "" : " source-state-down")}>
          {source.reachable ? "Answering" : "Not answering"}
        </span>
        <span className="source-badges">
          {source.remote && (
            <span
              className="badge badge-warn"
              title="Not on this machine. Every prompt a build sends it leaves this computer."
            >
              Another computer
            </span>
          )}
          <span className="badge">{ORIGIN_LABEL[source.origin]}</span>
          {source.key_hint && <span className="badge badge-mono">key {source.key_hint}</span>}
        </span>
      </div>
      <p className="source-address mono">
        {source.base_url}
        {source.version ? <span className="dim"> · {source.version}</span> : null}
      </p>

      {!source.reachable ? (
        <p className="field-hint source-note">
          {source.origin === "detected"
            ? "It answered earlier and has stopped. Start it again and rescan."
            : "Nothing answers at this address. Check it's running, and that the address and key are right."}
          {source.error && (
            <>
              {" "}
              <span className="dim">({source.error})</span>
            </>
          )}
        </p>
      ) : source.models.length === 0 ? (
        <p className="field-hint source-note">Serves no models yet. {source.add_model}</p>
      ) : (
        <ul className="model-rows" aria-label={`Models on ${source.label}`}>
          {source.models.map((m) => (
            <SourceModelRow
              key={m.spec}
              model={m}
              status={status}
              check={checks === null ? undefined : checks[m.spec] ?? null}
              onCheck={onCheck}
              busy={selecting !== null}
              selecting={selecting === m.spec}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}

      {source.reachable &&
        (source.can_download ? (
          <DownloadField source={source} onDone={onRefresh} />
        ) : source.models.length > 0 ? (
          <p className="field-hint source-note">
            To add a model: {source.add_model}
            {source.home && (
              <>
                {" "}
                <a className="link" href={source.home} target="_blank" rel="noreferrer">
                  {source.runtime_label ?? source.label} {Icon.external}
                </a>
              </>
            )}
          </p>
        ) : null)}

      {source.origin !== "configured" && (
        <div className="source-actions">
          {editingKey ? (
            <form
              className="source-key"
              onSubmit={(e) => {
                e.preventDefault();
                if (key.trim()) saveKey(key.trim());
              }}
            >
              <label htmlFor={`key-${source.id}`} className="sr-only">
                API key for {source.label}
              </label>
              <input
                id={`key-${source.id}`}
                type="password"
                autoComplete="off"
                className="input input-mono"
                placeholder={source.key_hint ? "Enter a new key to replace it" : "API key"}
                autoFocus
                value={key}
                disabled={savingKey}
                onChange={(e) => setKey(e.target.value)}
              />
              <button className="btn btn-sm btn-primary" type="submit" disabled={savingKey || !key.trim()}>
                {savingKey && <span className="btn-spinner" aria-hidden="true" />}
                Save key
              </button>
              {source.key_hint && (
                <button className="btn btn-sm" type="button" disabled={savingKey} onClick={() => saveKey("")}>
                  Clear key
                </button>
              )}
              <button
                className="btn btn-sm btn-ghost"
                type="button"
                onClick={() => {
                  focusNext.current = "key";
                  setEditingKey(false);
                }}
              >
                Cancel
              </button>
            </form>
          ) : (
            <button ref={keyButton} className="btn btn-sm btn-ghost" onClick={() => setEditingKey(true)}>
              {source.key_hint ? "Change key" : "Set a key"}
            </button>
          )}
          {source.removable &&
            (confirming ? (
              <span className="source-confirm" role="group" aria-label={`Remove ${source.label}?`}>
                <span className="field-hint">Remove {source.label}? Agents set to its models stop until you choose again.</span>
                <button className="btn btn-sm btn-danger" onClick={remove} disabled={removing}>
                  {removing && <span className="btn-spinner" aria-hidden="true" />}
                  Remove
                </button>
                <button
                  ref={keepButton}
                  className="btn btn-sm"
                  onClick={() => {
                    focusNext.current = "remove";
                    setConfirming(false);
                  }}
                  disabled={removing}
                >
                  Keep
                </button>
              </span>
            ) : (
              <button
                ref={removeButton}
                className="btn btn-sm btn-ghost"
                onClick={() => {
                  focusNext.current = "keep";
                  setConfirming(true);
                }}
              >
                {Icon.trash} Remove
              </button>
            ))}
        </div>
      )}
    </li>
  );
}

/**
 * A model a source serves; the row is the selection itself.
 *
 * Beside the name: whether it will run a build here, and a way to tune how it is
 * run. Both open inline under the row — one at a time, so a list of models never
 * turns into a wall of open panels.
 */
function SourceModelRow({
  model,
  status,
  check,
  onCheck,
  busy,
  selecting,
  onSelect,
}: {
  model: SourceModel;
  status: LocalStatus;
  /** undefined while the checks load; null when this model wasn't checked. */
  check: ModelCheck | null | undefined;
  onCheck: (spec: string, check: ModelCheck) => void;
  busy: boolean;
  selecting: boolean;
  onSelect: (spec: string) => void;
}) {
  const [open, setOpen] = useState<"check" | "tune" | null>(null);
  const panelId = useId();
  const tuneButton = useRef<HTMLButtonElement>(null);
  const current = model.spec === status.default_model;
  // Asked of the runtime, never of the name. A model that only makes embeddings is
  // a working model doing a different job: it is named and kept, and the one thing
  // it cannot be is the model eight agents write with. The badge prints what the
  // runtime actually said rather than a word for the kind of model we assume it is.
  const canBuild = canRunABuild(model.spec, status.cannot_build);
  const reported = status.model_capabilities?.[model.spec] ?? [];
  const embeds = status.embedding_model === model.spec;
  const toggle = (which: "check" | "tune") => setOpen((o) => (o === which ? null : which));
  return (
    <li className="model-item">
      <div className="model-row" data-current={current || undefined}>
        <span className="model-row-name mono">{model.name}</span>
        {!canBuild && (
          <span
            className="badge"
            title={`The runtime ${runtimeSays(reported)}. It can't write, so no agent can run on it — which is why it isn't offered as a build model.`}
          >
            {reported.length > 0 ? `${reported.join(" · ")} only` : "can't write"}
          </span>
        )}
        {embeds && (
          <span className="badge" title="Memory and document search embed with this model.">
            memory &amp; search
          </span>
        )}
        {!model.is_local && (
          <span
            className="badge badge-warn"
            title="This runtime sends the model to a hosted service to run. Local-only builds can't use it."
          >
            hosted
          </span>
        )}
        {canBuild && status.code_models.includes(model.spec) && (
          <span
            className="badge"
            title="Its name suggests it was trained on code — a guess from the name, not a measurement."
          >
            code
          </span>
        )}
        {canBuild && check !== null && (
          <VerdictChip
            check={check}
            expanded={open === "check"}
            onToggle={check ? () => toggle("check") : undefined}
            controls={panelId}
          />
        )}
        {canBuild && (
          <button
            ref={tuneButton}
            className="btn btn-sm btn-ghost"
            aria-expanded={open === "tune"}
            aria-controls={panelId}
            onClick={() => toggle("tune")}
            aria-label={`Tune how ${model.name} generates`}
          >
            {Icon.gear}
            Tune
          </button>
        )}
        {current ? (
          <span className="badge badge-ok">
            <span className="dot dot-ok" aria-hidden="true" />
            Default
          </span>
        ) : (
          <button
            className="btn btn-sm"
            onClick={() => onSelect(model.spec)}
            disabled={busy || !canBuild || !model.is_local}
            title={
              !canBuild
                ? `The runtime ${runtimeSays(reported)}. It can't write, so every agent would fail on its first call.`
                : !model.is_local
                  ? "It runs on a hosted service, so it can't be the local default. Pin it to one agent below instead."
                  : undefined
            }
            aria-label={`Run agents on ${model.name} by default`}
          >
            {selecting && <span className="btn-spinner" aria-hidden="true" />}
            Use this
          </button>
        )}
      </div>
      {open === "check" && check && (
        <div className="model-panel">
          <CheckDetail check={check} id={panelId} />
        </div>
      )}
      {open === "tune" && (
        <div className="model-panel">
          <ModelTune
            id={panelId}
            spec={model.spec}
            name={model.name}
            onSaved={(next) => onCheck(model.spec, next)}
            onClose={() => {
              setOpen(null);
              tuneButton.current?.focus();
            }}
          />
        </div>
      )}
    </li>
  );
}

/** Streams one download from a source whose runtime can do it through its API. */
function useDownload(source: LocalSource, onDone: () => void) {
  const [pulling, setPulling] = useState<string | null>(null);
  const [pct, setPct] = useState<number | null>(null);
  const [phase, setPhase] = useState("");
  const [error, setError] = useState("");

  async function pull(model: string) {
    const name = model.trim();
    if (!name) return false;
    setPulling(name);
    setError("");
    setPct(null);
    setPhase("Starting…");
    let failed = false;
    let finished = false;
    try {
      await api.pullLocalModel(source.id, name, (line) => {
        if (line.error) {
          failed = true;
          setError(line.error);
          return;
        }
        if (line.status) setPhase(line.status);
        if (line.status === "success") finished = true;
        if (line.total && line.completed) setPct(Math.round((line.completed / line.total) * 100));
      });
      if (!failed && !finished) {
        // The stream closed without the runtime saying it finished.
        failed = true;
        setError(`The download of ${name} stopped before ${source.label} said it was complete.`);
      }
      if (!failed) {
        setPhase("Done");
        onDone();
      }
    } catch (e: any) {
      failed = true;
      setError(e.message);
    } finally {
      setPulling(null);
    }
    return !failed;
  }

  return { pulling, pct, phase, error, pull };
}

function DownloadProgress({ pulling, pct, phase }: { pulling: string | null; pct: number | null; phase: string }) {
  if (!pulling) return null;
  return (
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
      <span className="field-hint" aria-live="polite">
        <span className="mono">{pulling}</span> · {phase}
        {pct !== null ? ` · ${pct}%` : ""}
      </span>
    </div>
  );
}

function DownloadButton({
  source,
  model,
  onDone,
  primary,
}: {
  source: LocalSource;
  model: string;
  onDone: () => void;
  primary?: boolean;
}) {
  const { pulling, pct, phase, error, pull } = useDownload(source, onDone);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, width: "100%" }}>
      <div>
        <button className={"btn btn-sm" + (primary ? " btn-primary" : "")} onClick={() => pull(model)} disabled={pulling !== null}>
          {pulling && <span className="btn-spinner" aria-hidden="true" />}
          {pulling ? "Downloading…" : `Download ${model}`}
        </button>
      </div>
      <DownloadProgress pulling={pulling} pct={pct} phase={phase} />
      {error && <span className="field-hint" style={{ color: "var(--bad)" }} role="alert">{error}</span>}
    </div>
  );
}

/** "Download another model" — only on a source whose runtime has a download API. */
function DownloadField({ source, onDone }: { source: LocalSource; onDone: () => void }) {
  const [wanted, setWanted] = useState("");
  const { pulling, pct, phase, error, pull } = useDownload(source, onDone);
  const id = `pull-${source.id}`;
  async function submit() {
    if (await pull(wanted)) setWanted("");
  }
  return (
    <div className="source-download">
      <div className="provider-grid">
        <div className="field" style={{ flex: 1, minWidth: 200 }}>
          <label htmlFor={id}>Download another model to {source.label}</label>
          <input
            id={id}
            className="input input-mono"
            placeholder="name:tag"
            value={wanted}
            disabled={pulling !== null}
            onChange={(e) => setWanted(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") submit();
            }}
          />
        </div>
        <button className="btn btn-primary" onClick={submit} disabled={pulling !== null || !wanted.trim()}>
          {pulling && <span className="btn-spinner" aria-hidden="true" />}
          {Icon.download} Download
        </button>
      </div>
      {source.library && (
        <p className="field-hint" style={{ marginTop: 8 }}>
          Anything from{" "}
          <a className="link" href={source.library} target="_blank" rel="noreferrer">
            {source.runtime_label ?? source.label}&apos;s library
          </a>{" "}
          works. It stays on that machine and costs nothing to run.
        </p>
      )}
      <DownloadProgress pulling={pulling} pct={pct} phase={phase} />
      {error && (
        <p className="field-hint" style={{ color: "var(--bad)", marginTop: 6 }} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * Ports on this machine that answered but match no runtime this app knows. Not used
 * until someone says what they are — an answer is not an identity, and `8080` is the
 * default of half a dozen products and of anything else a developer runs.
 */
function UnknownEndpoints({
  unknown,
  onChanged,
  onError,
}: {
  unknown: UnknownEndpoint[];
  onChanged: (next: LocalStatus) => void;
  onError: (message: string) => void;
}) {
  const [confirming, setConfirming] = useState<string | null>(null);
  if (unknown.length === 0) return null;

  async function confirm(url: string) {
    setConfirming(url);
    try {
      onChanged(await api.addSource({ base_url: url }));
    } catch (e: any) {
      onError(e.message);
    } finally {
      setConfirming(null);
    }
  }

  return (
    <div className="source-unknown">
      <div className="sec-head" style={{ marginBottom: 6 }}>
        <h3 className="label">Answered, but not recognised</h3>
        <span className="rule" />
      </div>
      <ul className="unknown-rows">
        {unknown.map((u) => (
          <li key={u.base_url} className="unknown-row">
            <span className="unknown-id">
              <span className="mono unknown-url">{hostOf(u.base_url)}</span>
              <span className="field-hint">{u.note}</span>
            </span>
            {u.openai ? (
              <button
                className="btn btn-sm"
                onClick={() => confirm(u.base_url)}
                disabled={confirming !== null}
                aria-label={`Use ${hostOf(u.base_url)} as an OpenAI-compatible source`}
              >
                {confirming === u.base_url && <span className="btn-spinner" aria-hidden="true" />}
                Use as OpenAI-compatible
              </button>
            ) : (
              <span className="badge">Ignored</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Add a source by address — the only way one on another computer is ever used. */
function AddSource({ onChanged }: { onChanged: (next: LocalStatus) => void }) {
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [label, setLabel] = useState("");
  const [key, setKey] = useState("");
  const [remoteOk, setRemoteOk] = useState(false);
  // The server decides what counts as this machine. When it says an address is not,
  // the confirmation is shown whatever this page's own reading of it was.
  const [serverSaysRemote, setServerSaysRemote] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [added, setAdded] = useState("");
  const trigger = useRef<HTMLButtonElement>(null);
  const reopen = useRef(false);
  const ids = useId();
  const id = (part: string) => `${ids}-${part}`;

  // Focus goes back to the button that opened the form, so closing it does not drop
  // a keyboard user at the top of the page.
  useEffect(() => {
    if (!open && reopen.current) {
      reopen.current = false;
      trigger.current?.focus();
    }
  }, [open]);

  const remote = url.trim() !== "" && (serverSaysRemote || !isLoopback(url.trim()));
  const ready = url.trim() !== "" && (!remote || remoteOk);

  function close() {
    reopen.current = true;
    setOpen(false);
    setError("");
  }

  async function submit() {
    if (!ready || saving) return;
    setSaving(true);
    setError("");
    setAdded("");
    try {
      onChanged(
        await api.addSource({
          base_url: url.trim(),
          label: label.trim() || undefined,
          api_key: key.trim() || undefined,
          confirm_remote: remote ? remoteOk : undefined,
        }),
      );
      setAdded(label.trim() || url.trim());
      setUrl("");
      setLabel("");
      setKey("");
      setRemoteOk(false);
      setServerSaysRemote(false);
      close();
    } catch (e: any) {
      if (/another computer/i.test(e.message ?? "")) setServerSaysRemote(true);
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="source-add">
      {/* Mounted once and kept, so the confirmation is announced when it changes. */}
      <p className="sr-only" aria-live="polite">
        {added ? `Added ${added}.` : ""}
      </p>
      {!open ? (
        <div className="source-add-closed">
          <button ref={trigger} className="btn btn-sm" onClick={() => setOpen(true)}>
            {Icon.plus} Add a source
          </button>
          <span className="field-hint">
            {added ? `Added ${added}.` : "A runtime on another port, another machine, or one that needs a key."}
          </span>
        </div>
      ) : (
        <form
          className="source-form"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
          aria-label="Add a model source"
        >
          <div className="source-form-grid">
            <div className="field source-form-url">
              <label htmlFor={id("url")}>Address</label>
              <input
                id={id("url")}
                className="input input-mono"
                placeholder="http://127.0.0.1:1234"
                value={url}
                autoFocus
                disabled={saving}
                onChange={(e) => {
                  setUrl(e.target.value);
                  setRemoteOk(false);
                  setServerSaysRemote(false);
                }}
                aria-describedby={id("url-hint")}
              />
              <span className="field-hint" id={id("url-hint")}>
                The server&apos;s root — a trailing <span className="mono">/v1</span> is fine.
              </span>
            </div>
            <div className="field">
              <label htmlFor={id("label")}>Name (optional)</label>
              <input
                id={id("label")}
                className="input"
                placeholder="GPU box"
                value={label}
                disabled={saving}
                onChange={(e) => setLabel(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor={id("key")}>API key (optional)</label>
              <input
                id={id("key")}
                type="password"
                autoComplete="off"
                className="input input-mono"
                placeholder="only if it asks for one"
                value={key}
                disabled={saving}
                onChange={(e) => setKey(e.target.value)}
              />
            </div>
          </div>

          {remote && (
            <label className="check-line notice notice-warn" htmlFor={id("remote")}>
              <input
                id={id("remote")}
                type="checkbox"
                checked={remoteOk}
                disabled={saving}
                onChange={(e) => setRemoteOk(e.target.checked)}
              />
              <span className="notice-body">
                <span className="notice-title">This address is another computer</span>
                <span className="notice-text">
                  Every prompt a build sends it — your idea, the plans, the code — leaves this
                  machine. Tick to confirm that is what you want.
                </span>
              </span>
            </label>
          )}

          <div className="source-form-actions">
            <button className="btn btn-primary" type="submit" disabled={!ready || saving}>
              {saving && <span className="btn-spinner" aria-hidden="true" />}
              {saving ? "Checking it answers…" : "Add source"}
            </button>
            <button className="btn btn-ghost" type="button" disabled={saving} onClick={close}>
              Cancel
            </button>
          </div>
          {error && (
            <p className="field-hint" style={{ color: "var(--bad)", marginTop: 8 }} role="alert">
              {error}
            </p>
          )}
        </form>
      )}
    </div>
  );
}

/**
 * What this model can actually do, asked of the model rather than assumed.
 *
 * The window here is the one the pipeline budgets every prompt for and sends with
 * every call — the same resolved number, not a second guess at it. It is shown because it used to be invisible:
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
        {profile.thinking && profile.thinking !== "none" && (
          <div className="fact">
            <dt>Thinking</dt>
            <dd>
              {profile.thinking_level ?? "model default"}
              {profile.reasoning_tokens ? (
                <span className="dim"> · {tokens(profile.reasoning_tokens)} kept for it</span>
              ) : null}
            </dd>
          </div>
        )}
      </dl>

      {/* Where the number came from, in one line — because "32,768" means something
          different when the model reported it than when nobody could. */}
      <p className="field-hint" style={{ marginTop: 12 }}>
        {profile.source === "probe" && profile.context_limit
          ? `Reported by its runtime; it supports up to ${profile.context_limit.toLocaleString()}.`
          : "Its runtime doesn't report a window, so the configured fallback is in force."}
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
        {state === null ? (
          "…"
        ) : state.default_model ? (
          <>
            <span className="mono">{modelName(state.default_model)}</span>
            {" "}on {sourceLabel(state, state.default_model)}
          </>
        ) : (
          "no model yet — no local runtime serves one that writes"
        )}
        . Give one its own model and that agent uses it from its next phase — no restart, no file
        to edit. Only models a source actually serves appear here.
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
                  You have <span className="mono">{modelName(coder)}</span> on{" "}
                  {state ? sourceLabel(state, coder) : "a source"}
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

/** A source's name for a spec, from the list the page already has. */
function sourceLabel(state: RoleSettings, spec: string): string {
  const id = spec.slice(0, Math.max(spec.indexOf(":"), 0));
  return state.sources.find((s) => s.id === id)?.label ?? id;
}

/** How a model reads in a picker: its name, and where it comes from. */
function optionLabel(state: RoleSettings, spec: string): string {
  const id = spec.slice(0, Math.max(spec.indexOf(":"), 0));
  const local = state.sources.some((s) => s.id === id);
  return local ? `${modelName(spec)} · ${sourceLabel(state, spec)}` : spec;
}

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
  const embeddings = row.role === "embeddings";

  // A saved choice the list no longer offers — a model removed from its runtime,
  // or a runtime that has stopped. Kept as an option so the control shows what this
  // role is actually set to, rather than snapping back to a default it is not using.
  const options = useMemo(() => {
    // Embeddings come from a model source and nothing else, and it is the one role
    // an embedding-only model is the *right* answer for — so the capability filter
    // below deliberately does not apply. When no runtime says which of its models
    // embed, every local model is offered rather than none.
    if (embeddings) {
      const all = state.embedding_models.length > 0 ? [...state.embedding_models] : [...state.local_models];
      return row.assigned && !all.includes(row.assigned) ? [...all, row.assigned] : all;
    }
    // Every other role is an agent that has to write. Same rule and same map as the
    // build picker, so a model missing from one is missing from both.
    const all = [
      ...state.local_models.filter((m) => canRunABuild(m, state.cannot_build)),
      ...state.cloud_models,
    ];
    return row.assigned && !all.includes(row.assigned) ? [...all, row.assigned] : all;
  }, [state.local_models, state.cloud_models, state.cannot_build, state.embedding_models, row.assigned, embeddings]);
  // A local choice no running source serves. A cloud choice is not checked here:
  // nothing can confirm one without spending a call.
  const cloud = PROVIDERS.some((p) => p.key === row.provider);
  const missing = Boolean(row.assigned) && !cloud && !state.local_models.includes(row.assigned ?? "");
  // The explicit list above is filtered, but "Default" is the value every unpinned
  // row holds — so a default that cannot write would otherwise sit in each select
  // reading as a perfectly good choice. Embeddings is exempt for the same reason it
  // is exempt from the filter.
  const defaultCannotWrite =
    !embeddings && Boolean(state.default_model) && !canRunABuild(state.default_model ?? "", state.cannot_build);
  // A role pinned to such a model before this check existed (or through the API)
  // is still shown as set — hiding it would misreport the role — but it is said
  // plainly, since a build refuses to start while any agent is set to it.
  const assignedCannotWrite =
    !embeddings && Boolean(row.assigned) && !canRunABuild(row.assigned ?? "", state.cannot_build);

  // What "Automatic" means is what it would pick — not what a choice picked.
  const defaultText = embeddings
    ? state.embedding_automatic
      ? `Automatic — ${optionLabel(state, state.embedding_automatic)}`
      : "Automatic — none found, so memory and search are off"
    : state.default_model
      ? `Default — ${optionLabel(state, state.default_model)}${defaultCannotWrite ? " · can't write" : ""}`
      : "Default — none available";

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
          <option value="">{defaultText}</option>
          {options.map((m) => (
            <option key={m} value={m}>
              {optionLabel(state, m)}
              {assignedCannotWrite && m === row.assigned ? " · can't write" : ""}
            </option>
          ))}
        </select>
        {busy && <span className="btn-spinner" aria-hidden="true" />}
      </span>
      {assignedCannotWrite && (
        <span className="role-warn" role="status">
          {Icon.alert}
          <span>
            <span className="mono">{modelName(row.assigned)}</span> can&apos;t write — a build won&apos;t
            start while this agent is set to it.
          </span>
        </span>
      )}
      {missing && (
        <span className="role-warn" role="status">
          {Icon.alert}
          <span>
            <span className="mono">{modelName(row.assigned)}</span> isn&apos;t served by any running
            source —{" "}
            {embeddings
              ? "memory and document search are off until it is."
              : "a build using this agent won't start until it is."}
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
