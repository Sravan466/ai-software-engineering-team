"use client";

import { useId, useState, type ReactNode } from "react";
import { ApiError, type Connector, type ConnectorCheck, type ConnectorProblem, type ConnectorSaveResult, type ConnectorVar } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { BRAND_MARKS } from "./brand";

/**
 * The pieces every connector surface shares — the Connectors tab, the build's
 * "Connect your services" card, and the project's Connectors panel — so connecting
 * Stripe looks and behaves the same wherever it happens.
 *
 * The form is drawn in the database card's vocabulary (`db-*`): guide on the left,
 * fields on the right, a status line that says what the test found. A key goes in
 * and never comes back — the page is only ever handed `…last4`.
 */

// ── marks ────────────────────────────────────────────────────────────────────
export function ConnectorMark({ id, label, size = "md" }: { id: string; label: string; size?: "sm" | "md" | "lg" }) {
  const mark = BRAND_MARKS[id];
  return (
    <span className={`cx-mark cx-mark-${size}`} aria-hidden="true">
      {mark ? (
        <svg viewBox="0 0 24 24" fill="currentColor" stroke="none" fillRule={mark.evenodd ? "evenodd" : undefined}>
          {mark.paths.map((d, i) => (
            <path key={i} d={d} />
          ))}
        </svg>
      ) : (
        <span className="cx-mono">{label.replace(/[^A-Za-z0-9]/g, "").slice(0, 2)}</span>
      )}
    </span>
  );
}

/** What a connector's state reads as, in one badge. Never colour alone: always words. */
export function statusOf(c: Pick<Connector, "connected" | "check" | "mode" | "saved" | "connectable" | "wave">): {
  cls: string;
  text: string;
} | null {
  if (!c.connectable) return { cls: "cx-badge-soon", text: c.wave === 2 ? "Coming next" : "Coming soon" };
  if (!c.connected) return null;
  const hint = c.saved?.find((s) => s.hint.startsWith("…"))?.hint;
  if (c.check?.status === "unchecked") return { cls: "badge-warn", text: "Saved, not tested" };
  if (c.mode === "live") return { cls: "badge-bad", text: "Connected · Live" };
  const mode = c.mode === "test" ? " · Test mode" : "";
  return { cls: "badge-ok", text: `Connected${mode}${hint ? ` · ${hint}` : ""}` };
}

export function StatusBadge({ c }: { c: Parameters<typeof statusOf>[0] }) {
  const s = statusOf(c);
  if (!s) return null;
  const dot = s.cls === "badge-ok" ? "dot-ok" : s.cls === "badge-bad" ? "dot-bad" : s.cls === "badge-warn" ? "dot-warn" : "";
  return (
    <span className={`badge ${s.cls}`}>
      {dot && <span className={`dot ${dot}`} aria-hidden="true" />}
      {s.text}
    </span>
  );
}

export function VarChips({ variables }: { variables: ConnectorVar[] }) {
  return (
    <ul className="cx-vars" aria-label="Variables the code reads">
      {variables.map((v) => (
        <li key={v.name}>
          <code className="db-var">{v.name}</code>
          <span className={`cx-side cx-side-${v.side}`}>{v.side === "client" ? "public" : "server"}</span>
        </li>
      ))}
    </ul>
  );
}

// ── the form ─────────────────────────────────────────────────────────────────
type Outcome =
  | { kind: "idle" }
  | { kind: "saved"; check: ConnectorCheck }
  | { kind: "failed"; check: ConnectorCheck }
  | { kind: "invalid"; problems: ConnectorProblem[] }
  | { kind: "live"; message: string }
  | { kind: "error"; message: string };

/** A model setting (`OPENAI_MODEL`): picked from the key's model list once it's tested. */
const isModelVar = (v: ConnectorVar) => /_MODEL$/.test(v.name) && !v.secret;

export function ConnectForm({
  connector,
  onSave,
  onRetest,
  onRemove,
  removeLabel = "Disconnect",
  removeConfirm,
  extra,
  actions,
  secondary,
  busy = false,
  onSaved,
}: {
  connector: Connector;
  /** Test and save. The caller decides where it goes: the account, or this build. */
  onSave: (values: Record<string, string>, confirmLive: boolean) => Promise<ConnectorSaveResult>;
  onRetest?: () => Promise<ConnectorSaveResult>;
  onRemove?: () => Promise<void>;
  removeLabel?: string;
  /** Said before removing: which builds will notice. Asked inline, never in a modal. */
  removeConfirm?: ReactNode;
  /** Under the fields, above the buttons — e.g. "Save to my Connectors". */
  extra?: ReactNode;
  /** The primary action once something is saved (Continue build, at the gate). */
  actions?: ReactNode;
  /** Always beside Test and save, so nobody is stuck. */
  secondary?: ReactNode;
  busy?: boolean;
  onSaved?: (r: ConnectorSaveResult) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [shown, setShown] = useState<Record<string, boolean>>({});
  const [testing, setTesting] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>({ kind: "idle" });
  const [replacing, setReplacing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [flash, setFlash] = useState<number | null>(null);
  const base = useId();

  const saved = connector.saved ?? [];
  const hasSaved = connector.connected && saved.length > 0 && !replacing;
  // The model is chosen from the key's own list, after it's tested — not typed blind.
  // A mirrored variable (the browser's copy of a public id) is filled by the server.
  const fields = connector.variables.filter((v) => !isModelVar(v) && !v.copy_of);
  const modelVar = connector.variables.find(isModelVar);
  const models = connector.models ?? [];

  function problemFor(name: string) {
    return outcome.kind === "invalid" ? outcome.problems.find((p) => p.name === name)?.message : undefined;
  }

  async function run(fn: () => Promise<ConnectorSaveResult>) {
    setTesting(true);
    setOutcome({ kind: "idle" });
    try {
      const r = await fn();
      if (r.status === "invalid") {
        setOutcome({ kind: "invalid", problems: r.problems ?? [] });
        const first = r.problems?.[0]?.name;
        if (first) document.getElementById(`${base}-${first}`)?.focus();
      } else if (r.ok && r.check) {
        setOutcome({ kind: "saved", check: r.check });
        setValues({});
        setReplacing(false);
        onSaved?.(r);
      } else if (r.check) {
        setOutcome({ kind: "failed", check: r.check });
      }
      return r;
    } catch (e: any) {
      const detail = e instanceof ApiError ? (e.data?.detail as any) : null;
      if (e instanceof ApiError && e.status === 409 && detail?.status === "needs_live_confirm") {
        setOutcome({ kind: "live", message: detail.message });
      } else if (e instanceof ApiError && e.status === 429) {
        setOutcome({ kind: "error", message: "Too many tests in a few minutes. Wait a little, then try again — nothing was saved." });
      } else {
        setOutcome({ kind: "error", message: e?.message || "The test didn't finish. Try again." });
      }
    } finally {
      setTesting(false);
    }
  }

  // A choice that is saved shows as saved, not as the default — and the one shown is
  // always sent, so "replace" never checks new keys against an old environment.
  const savedChoice = (v: ConnectorVar) =>
    v.options?.includes(saved.find((s) => s.name === v.name)?.hint ?? "") ? saved.find((s) => s.name === v.name)!.hint : undefined;
  const choiceOf = (v: ConnectorVar) => values[v.name] || savedChoice(v) || v.options![0];
  const sent = () => {
    const out = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim()));
    for (const v of fields) if (v.options?.length) out[v.name] = choiceOf(v);
    return out;
  };

  // A model setting typed by hand, for a provider whose key check lists no models.
  const [typedModel, setTypedModel] = useState("");
  const save = (confirmLive = false) => run(() => onSave(sent(), confirmLive));

  function switchToTest() {
    setValues({});
    setOutcome({ kind: "idle" });
    // After the panel unmounts, so focus lands on the first key field, not on <body>.
    window.requestAnimationFrame(() => document.getElementById(`${base}-${fields[0]?.name}`)?.focus());
  }

  function showStep(step: number | null | undefined) {
    if (!step) return;
    setFlash(step);
    document.getElementById(`${base}-step-${step}`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    window.setTimeout(() => setFlash(null), 1600);
  }

  async function remove() {
    if (!onRemove) return;
    setRemoving(true);
    try {
      await onRemove();
      setConfirming(false);
      setOutcome({ kind: "idle" });
    } catch (e: any) {
      setOutcome({ kind: "error", message: e?.message || "Couldn't remove it. Try again." });
    } finally {
      setRemoving(false);
    }
  }

  const failedName = outcome.kind === "failed" ? outcome.check.name ?? fields[0]?.name : undefined;
  const lockedOut = busy || testing;

  return (
    <div className="db-form cx-form">
      <div className="db-grid">
        <div className="db-guide">
          <h3 className="db-guide-title">Where to find it · {connector.label}</h3>
          <ol className="db-steps">
            {connector.guide.map((s, i) => (
              <li key={i} id={`${base}-step-${i + 1}`} className={flash === i + 1 ? "db-step-flash" : undefined}>
                {s.text}
              </li>
            ))}
          </ol>
          {connector.dashboard_url && (
            <a className="btn btn-sm" href={connector.dashboard_url} target="_blank" rel="noopener noreferrer">
              Open {connector.label} dashboard {Icon.external}
            </a>
          )}
        </div>

        <div className="db-fields">
          {hasSaved ? (
            <div className="db-saved">
              <ul className="db-saved-list">
                {saved.map((s) => (
                  <li key={s.name}>
                    <code className="db-var">{s.name}</code>
                    <span className="db-hint mono">{s.hint}</span>
                  </li>
                ))}
              </ul>
              {modelVar && models.length === 0 && (
                <div className="field cx-model">
                  <label htmlFor={`${base}-model-typed`}>
                    {modelVar.label} <code className="db-var">{modelVar.name}</code>
                  </label>
                  <div className="db-input-row">
                    <input
                      id={`${base}-model-typed`}
                      className="input input-mono db-input"
                      value={typedModel}
                      placeholder={saved.find((s) => s.name === modelVar.name)?.hint || "The model id the app calls"}
                      autoComplete="off"
                      spellCheck={false}
                      disabled={lockedOut}
                      onChange={(e) => setTypedModel(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn"
                      disabled={lockedOut || !typedModel.trim()}
                      onClick={() =>
                        run(() => onSave({ [modelVar.name]: typedModel.trim() }, false)).then((r) => {
                          // Kept on a refusal, so it can be corrected rather than retyped.
                          if (r?.ok) setTypedModel("");
                        })
                      }
                    >
                      Save
                    </button>
                  </div>
                  <p className="field-hint">This key&apos;s check doesn&apos;t list models, so type the id. The app reads it from {modelVar.name}.</p>
                </div>
              )}
              {modelVar && models.length > 0 && (
                <ModelPicker
                  id={`${base}-model`}
                  v={modelVar}
                  models={models}
                  current={saved.find((s) => s.name === modelVar.name)?.hint}
                  disabled={lockedOut}
                  onPick={(m) => run(() => onSave({ [modelVar.name]: m }, false))}
                />
              )}
              {confirming ? (
                <div className="notice notice-warn cx-confirm" role="group" aria-label={`${removeLabel} ${connector.label}`}>
                  {Icon.alert}
                  <div className="notice-body">
                    <span className="notice-title">{removeLabel} {connector.label}?</span>
                    {removeConfirm && <span className="notice-text">{removeConfirm}</span>}
                    <div className="notice-actions">
                      <button type="button" className="btn btn-sm btn-danger" onClick={remove} disabled={removing}>
                        {removing ? <span className="btn-spinner" aria-hidden="true" /> : Icon.trash} {removeLabel}
                      </button>
                      <button type="button" className="btn btn-sm btn-ghost" onClick={() => setConfirming(false)} disabled={removing}>
                        Keep it
                      </button>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="db-saved-actions">
                  <button type="button" className="btn btn-sm" onClick={() => setReplacing(true)} disabled={lockedOut}>
                    {Icon.pen} Replace
                  </button>
                  {onRetest && (
                    <button type="button" className="btn btn-sm" onClick={() => run(onRetest)} disabled={lockedOut}>
                      {Icon.refresh} Re-test
                    </button>
                  )}
                  {onRemove && (
                    <button type="button" className="btn btn-sm btn-danger" onClick={() => setConfirming(true)} disabled={lockedOut}>
                      {Icon.trash} {removeLabel}
                    </button>
                  )}
                </div>
              )}
            </div>
          ) : (
            fields.map((v) => {
              const fieldId = `${base}-${v.name}`;
              const hintId = `${fieldId}-hint`;
              const bad = problemFor(v.name);
              const failedHere = failedName === v.name && outcome.kind === "failed";
              const masked = v.secret && !shown[v.name];
              return (
                <div className="field db-field" key={v.name}>
                  {/* A choice is a radio group, labelled by this — a <label> would click its first option. */}
                  {(() => {
                    const inner = (
                      <>
                        {v.label}
                        {!v.required && <span className="db-optional">optional</span>}
                        {v.side === "client" && <span className="cx-public">public — goes in the browser</span>}
                        <code className="db-var">{v.name}</code>
                      </>
                    );
                    return v.options?.length ? (
                      <span className="cx-flabel" id={`${fieldId}-label`}>{inner}</span>
                    ) : (
                      <label htmlFor={fieldId}>{inner}</label>
                    );
                  })()}
                  {v.options?.length ? (
                    <div
                      className="seg cx-choice"
                      role="radiogroup"
                      aria-labelledby={`${fieldId}-label`}
                      aria-describedby={hintId}
                      onKeyDown={(e) => {
                        const keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"];
                        if (!keys.includes(e.key)) return;
                        e.preventDefault();
                        const opts = v.options!;
                        const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : -1;
                        const next = opts[(opts.indexOf(choiceOf(v)) + step + opts.length) % opts.length];
                        setValues((prev) => ({ ...prev, [v.name]: next }));
                        window.requestAnimationFrame(() =>
                          document.getElementById(`${fieldId}-${opts.indexOf(next)}`)?.focus(),
                        );
                      }}
                    >
                      {v.options.map((o, i) => {
                        const on = choiceOf(v) === o;
                        return (
                          <button
                            key={o}
                            type="button"
                            role="radio"
                            className="seg-btn"
                            aria-checked={on}
                            // One Tab stop for the group: the checked option; arrows move within.
                            tabIndex={on ? 0 : -1}
                            id={`${fieldId}-${i}`}
                            disabled={testing}
                            onClick={() => {
                              setValues((prev) => ({ ...prev, [v.name]: o }));
                              if (outcome.kind !== "idle" && outcome.kind !== "saved") setOutcome({ kind: "idle" });
                            }}
                          >
                            {choiceLabel(o)}
                          </button>
                        );
                      })}
                    </div>
                  ) : (
                  <div className="db-input-row">
                    <input
                      id={fieldId}
                      className="input input-mono db-input"
                      type={masked ? "password" : "text"}
                      value={values[v.name] ?? ""}
                      placeholder={v.placeholder}
                      autoComplete="off"
                      spellCheck={false}
                      autoCapitalize="off"
                      aria-describedby={hintId}
                      aria-invalid={Boolean(bad || failedHere) || undefined}
                      disabled={testing}
                      onChange={(e) => {
                        setValues((prev) => ({ ...prev, [v.name]: e.target.value }));
                        if (outcome.kind !== "idle" && outcome.kind !== "saved") setOutcome({ kind: "idle" });
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") save();
                      }}
                    />
                    {v.secret && (
                      <button
                        type="button"
                        className="btn db-reveal"
                        aria-pressed={Boolean(shown[v.name])}
                        aria-label={shown[v.name] ? `Hide ${v.label}` : `Show ${v.label}`}
                        onClick={() => setShown((s) => ({ ...s, [v.name]: !s[v.name] }))}
                      >
                        {shown[v.name] ? Icon.eyeOff : Icon.eye}
                        <span>{shown[v.name] ? "Hide" : "Show"}</span>
                      </button>
                    )}
                  </div>
                  )}
                  <div id={hintId} className="db-under">
                    {bad ? (
                      <p className="db-msg db-msg-bad" role="alert">
                        {Icon.alert}
                        <span>
                          {bad}
                          <StepLink step={outcome.kind === "invalid" ? outcome.problems.find((p) => p.name === v.name)?.step : null} onShow={showStep} />
                        </span>
                      </p>
                    ) : failedHere && outcome.kind === "failed" ? (
                      <p className="db-msg db-msg-bad" role="alert">
                        {Icon.alert}
                        <span>
                          {outcome.check.message}
                          <StepLink step={outcome.check.step} onShow={showStep} />
                        </span>
                      </p>
                    ) : v.help ? (
                      <p className="field-hint">{v.help}</p>
                    ) : null}
                  </div>
                </div>
              );
            })
          )}

          {outcome.kind === "live" && (
            <div className="notice notice-warn cx-live" role="alert">
              {Icon.alert}
              <div className="notice-body">
                <span className="notice-title">This is a live key</span>
                <span className="notice-text">{outcome.message} Test mode is safer while you build.</span>
                <div className="notice-actions">
                  <button type="button" className="btn btn-sm btn-accent" onClick={() => save(true)} disabled={testing}>
                    Use live key
                  </button>
                  <button type="button" className="btn btn-sm" onClick={switchToTest} disabled={testing}>
                    Switch to test key
                  </button>
                </div>
              </div>
            </div>
          )}

          <div className="db-status" aria-live="polite">
            {testing ? (
              <p className="db-msg db-msg-run">
                <span className="btn-spinner" aria-hidden="true" />
                <span>Testing with {connector.label}…</span>
              </p>
            ) : outcome.kind === "saved" ? (
              <CheckLine check={outcome.check} />
            ) : outcome.kind === "error" ? (
              <p className="db-msg db-msg-bad" role="alert">
                {Icon.alert}
                <span>{outcome.message}</span>
              </p>
            ) : outcome.kind === "failed" && !failedName ? (
              <p className="db-msg db-msg-bad" role="alert">
                {Icon.alert}
                <span>{outcome.check.message}</span>
              </p>
            ) : hasSaved && connector.check ? (
              <CheckLine check={connector.check} />
            ) : null}
          </div>

          {!hasSaved && extra}

          <div className="db-actions">
            {hasSaved ? (
              actions
            ) : (
              <button type="button" className="btn btn-lg btn-accent" onClick={() => save()} disabled={lockedOut}>
                {testing ? <span className="btn-spinner" aria-hidden="true" /> : Icon.check}
                {testing ? "Testing…" : "Test and save"}
              </button>
            )}
            {replacing && saved.length > 0 && (
              <button type="button" className="btn btn-ghost" onClick={() => setReplacing(false)} disabled={testing}>
                Keep the saved one
              </button>
            )}
            {secondary}
          </div>
        </div>
      </div>
    </div>
  );
}

/** `https://eu.i.posthog.com` → `EU`, `sandbox` → `Sandbox`: a choice in words. */
function choiceLabel(option: string): string {
  const region = option.match(/^https:\/\/(us|eu)\./);
  if (region) return region[1].toUpperCase();
  if (/^(us|eu)$/.test(option)) return option.toUpperCase();
  return option.charAt(0).toUpperCase() + option.slice(1);
}

function StepLink({ step, onShow }: { step?: number | null; onShow: (n: number) => void }) {
  if (!step) return null;
  return (
    <>
      {" "}
      <button type="button" className="db-steplink" onClick={() => onShow(step)}>
        See step {step}
      </button>
    </>
  );
}

export function CheckLine({ check }: { check: ConnectorCheck }) {
  if (check.status === "connected") {
    return (
      <p className="db-msg db-msg-ok">
        {Icon.check}
        <span>
          {check.message}
          {check.latency_ms != null && <> · {check.latency_ms} ms</>}
        </span>
      </p>
    );
  }
  if (check.status === "failed") {
    return (
      <p className="db-msg db-msg-bad">
        {Icon.alert}
        <span>{check.message}</span>
      </p>
    );
  }
  return (
    <p className="db-msg db-msg-warn">
      {Icon.alert}
      <span>{check.message || "Saved, not tested. The crew builds against it anyway."}</span>
    </p>
  );
}

function ModelPicker({
  id,
  v,
  models,
  current,
  disabled,
  onPick,
}: {
  id: string;
  v: ConnectorVar;
  models: string[];
  current?: string;
  disabled: boolean;
  onPick: (model: string) => void;
}) {
  return (
    <div className="field cx-model">
      <label htmlFor={id}>
        {v.label} <code className="db-var">{v.name}</code>
      </label>
      <select
        id={id}
        className="select input-mono"
        value={current ?? ""}
        disabled={disabled}
        onChange={(e) => e.target.value && onPick(e.target.value)}
      >
        <option value="" disabled>
          Pick the model the app calls…
        </option>
        {models.map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
      <p className="field-hint">From your account&apos;s own list. The app reads it from {v.name} — never a hardcoded name.</p>
    </div>
  );
}

/** The one line every surface ends on: what the crew sees, and what it never does. */
export function LockLine({ names }: { names: string[] }) {
  return (
    <p className="db-lock">
      {Icon.lock}
      <span>
        Encrypted on this computer. The crew only ever sees the {names.length === 1 ? "name" : "names"}{" "}
        {names.map((n, i) => (
          <span key={n}>
            {i > 0 && (i === names.length - 1 ? " and " : ", ")}
            <code className="db-var">{n}</code>
          </span>
        ))}
        , never the value.
      </span>
    </p>
  );
}
