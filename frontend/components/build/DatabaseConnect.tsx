"use client";

import { useCallback, useEffect, useId, useState, type ReactNode } from "react";
import {
  api,
  ApiError,
  type DatabaseCheck,
  type DatabaseProblem,
  type DatabaseState,
  type DatabaseVar,
  type Project,
} from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { databaseEnv } from "@/lib/database";

/**
 * Connecting the database Atlas picked — at the gate right after the architecture,
 * or from the Deliver tab any time after.
 *
 * Two answers, and neither is a trap: paste the connection string (guided, tested
 * before it is saved) or carry on and add it later. Values go in and never come
 * back: the page is only ever handed a host or `…last4`, and the crew only ever
 * sees the variable names.
 */

type Act = (fn: () => Promise<unknown>) => Promise<boolean>;

// ── where to find it, per provider ───────────────────────────────────────────
// Step numbers are load-bearing: the server points a failure at "step 2", and
// `_STEPS` in `backend/app/build/dbconnect.py` is kept in step with these lists.
type Guide = { title: string; open?: { label: string; href: string }; steps: ReactNode[] };

const code = (text: string) => <code className="db-code">{text}</code>;
const path = (...parts: string[]) => (
  <span className="db-path">
    {parts.map((p, i) => (
      <span key={i}>
        {i > 0 && <span className="db-path-sep" aria-hidden="true">→</span>}
        <b>{p}</b>
      </span>
    ))}
  </span>
);

function guideFor(database: string, provider: string): Guide {
  switch (provider) {
    case "atlas":
      return {
        title: "MongoDB Atlas",
        open: { label: "Open MongoDB Atlas", href: "https://cloud.mongodb.com" },
        steps: [
          <>{path("Security", "Database Access")} — add a database user and note its password.</>,
          <>{path("Security", "Network Access")} — add your current IP address.</>,
          <>{path("Clusters", "Connect", "Drivers")} — copy the {code("mongodb+srv://…")} string and replace {code("<password>")} with that user&apos;s password.</>,
        ],
      };
    case "supabase":
      return {
        title: "Supabase",
        open: { label: "Open Supabase", href: "https://supabase.com/dashboard" },
        steps: [
          <>{path("Project Settings", "API Keys")} — copy the Project URL and the <b>publishable</b> key.</>,
          <>Optional, for direct Postgres: open {path("Connect", "Session pooler")}, copy the string and fill in your database password. The pooler works over IPv4.</>,
          <>Never paste the {code("service_role")} or {code("sb_secret_")} key here. It bypasses your security rules.</>,
        ],
      };
    case "neon":
      return {
        title: "Neon",
        open: { label: "Open Neon", href: "https://console.neon.tech" },
        steps: [
          <>{path("Project dashboard", "Connect")} — choose the branch, database and role.</>,
          <>Turn on <b>Connection pooling</b> and copy the string. It already ends in {code("sslmode=require")}.</>,
        ],
      };
    case "planetscale":
      return {
        title: "PlanetScale",
        open: { label: "Open PlanetScale", href: "https://app.planetscale.com" },
        steps: [
          <>Open your database and choose <b>Connect</b>.</>,
          <><b>Create password</b> and copy it now. It&apos;s shown only once.</>,
          <>Pick your framework and copy the {code("mysql://…")} string.</>,
        ],
      };
    case "firebase":
      return {
        title: "Firebase",
        open: { label: "Open Firebase", href: "https://console.firebase.google.com" },
        steps: [
          <>{path("Project settings", "Your apps")} — the gear icon, top left.</>,
          <>Pick the web app, then {path("SDK setup and configuration", "Config")}.</>,
          <>Copy the whole {code("firebaseConfig")} object, braces included, and paste it below.</>,
        ],
      };
    case "aws":
      return {
        title: "DynamoDB",
        open: { label: "Open AWS IAM", href: "https://console.aws.amazon.com/iam" },
        steps: [
          <>{path("IAM", "Users")} — create a user with DynamoDB access for this app.</>,
          <>{path("Security credentials", "Create access key")} — copy both halves; the secret is shown once.</>,
          <>The region is in the top-right of the console, e.g. {code("us-east-1")}.</>,
        ],
      };
    default: {
      const scheme = database === "mongodb" ? "mongodb" : database === "mysql" ? "mysql" : "postgresql";
      const port = database === "mongodb" ? "27017" : database === "mysql" ? "3306" : "5432";
      return {
        title: database === "mongodb" ? "MongoDB" : database === "mysql" ? "MySQL" : "Postgres",
        steps: [
          <>Create a database user for this app, with its own password.</>,
          <>
            Put the parts together:
            <span className="db-anatomy" aria-label="connection string parts">
              <span data-part="scheme">{scheme}://</span>
              <span data-part="user">user</span>:<span data-part="password">password</span>@
              <span data-part="host">host</span>:<span data-part="port">{port}</span>/
              <span data-part="db">app</span>
            </span>
          </>,
          <>Allow this computer&apos;s IP through the database&apos;s firewall.</>,
        ],
      };
    }
  }
}

// ── checks the page can make before the server does ──────────────────────────
const PLACEHOLDER = /<\s*(db_)?password\s*>|\[\s*your[-_ ]password\s*\]/i;

function blurCheck(v: DatabaseVar, raw: string): { bad?: string; note?: string } {
  const text = raw.trim();
  if (!text) return {};
  if (PLACEHOLDER.test(text)) {
    return { bad: "It still says <password>. Replace it with your database user's password." };
  }
  if (v.kind === "uri") {
    const body = text.split("://")[1] ?? "";
    const userinfo = body.includes("@") ? body.slice(0, body.lastIndexOf("@")) : "";
    const password = userinfo.includes(":") ? userinfo.slice(userinfo.indexOf(":") + 1) : "";
    if (/[@:/?#]/.test(password)) {
      const which = password.includes("@") ? "an @" : "a character like : / ? #";
      return { note: `Your password has ${which}. We'll encode it for you.` };
    }
  }
  return {};
}

// ── state ────────────────────────────────────────────────────────────────────
function useDatabase(id: string, refreshKey?: unknown) {
  const [state, setState] = useState<DatabaseState | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    api
      .getDatabase(id)
      .then((s) => {
        setState(s);
        setError("");
      })
      .catch((e: Error) => setError(e.message));
  }, [id]);
  useEffect(load, [load, refreshKey]);
  return { state, setState, error, reload: load };
}

// ── the gate ─────────────────────────────────────────────────────────────────
export function DatabaseGate({
  project,
  id,
  busy,
  act,
}: {
  project: Project;
  id: string;
  busy: boolean;
  act: Act;
}) {
  const { state, setState, error, reload } = useDatabase(id);
  const [open, setOpen] = useState(false);
  const [going, setGoing] = useState<"later" | "continue" | null>(null);
  const panelId = useId();

  const label = state?.database_label ?? project.charter?.database?.label ?? "a database";
  // A named host ("Supabase", "MongoDB Atlas") — never the generic one, which would
  // read "PostgreSQL on Postgres".
  const provider =
    state?.provider && state.provider !== "generic"
      ? state.providers?.find((p) => p.provider === state.provider)?.label
      : undefined;
  const names = state?.contract?.variables.map((v) => v.name) ?? databaseEnv(project);
  const saved = Boolean(state?.saved?.length) &&
    (project.database_status === "connected" || project.database_status === "unchecked" ||
      state?.status === "connected" || state?.status === "unchecked");

  async function later() {
    setGoing("later");
    await act(() => api.databaseLater(id));
    setGoing(null);
  }
  async function carryOn() {
    setGoing("continue");
    await act(() => api.databaseContinue(id));
    setGoing(null);
  }

  return (
    <section className="decision decision-database" aria-labelledby="decision-title">
      <header className="decision-head">
        <span className="decision-mark" aria-hidden="true">{Icon.database}</span>
        <div className="decision-headings">
          <h2 id="decision-title">Connect your database</h2>
          <p>
            Atlas picked{" "}
            {provider && provider !== label && provider.includes(label) ? (
              <b className="db-strong">{provider}</b>
            ) : (
              <>
                <b className="db-strong">{label}</b>
                {provider && provider !== label ? <> on <b className="db-strong">{provider}</b></> : null}
              </>
            )}{" "}
            for this build. Connect yours now so the crew builds against it, or keep going and add it
            any time.
          </p>
        </div>
        <span className="badge badge-mono db-token">
          {Icon.database}
          {state?.database ?? project.charter?.database?.token ?? "database"}
        </span>
      </header>

      <div className="db-choices">
        <button
          type="button"
          className="db-choice db-choice-paste"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((o) => !o)}
          disabled={busy && !open}
        >
          <span className="db-choice-icon" aria-hidden="true">{Icon.api}</span>
          <span className="db-choice-text">
            <span className="db-choice-title">Paste the database URI or API</span>
            <span className="db-choice-sub">
              Step-by-step for {provider ?? label}. Tested before it&apos;s saved.
            </span>
          </span>
          <span className="db-choice-chev" aria-hidden="true">{Icon.chevron}</span>
        </button>
        <button
          type="button"
          className="db-choice db-choice-later"
          onClick={later}
          disabled={busy}
          aria-busy={going === "later"}
        >
          <span className="db-choice-icon" aria-hidden="true">
            {going === "later" ? <span className="btn-spinner" /> : Icon.arrowRight}
          </span>
          <span className="db-choice-text">
            <span className="db-choice-title">Continue, I&apos;ll add it later</span>
            <span className="db-choice-sub">
              The crew builds with a placeholder. Connect it from the project anytime.
            </span>
          </span>
        </button>
      </div>

      <div className="db-disclosure" data-open={open} id={panelId}>
        <div
          className="db-disclosure-inner"
          // React 18 passes `inert` through only as a string; closed, nothing in it is tabbable.
          {...(open ? {} : ({ inert: "" } as Record<string, string>))}
        >
          {error ? (
            <div className="db-pad">
              <LoadError message={error} onRetry={reload} />
            </div>
          ) : !state ? (
            <div className="db-pad">
              <SkeletonLines lines={4} />
            </div>
          ) : (
            <DatabaseForm
              id={id}
              state={state}
              onState={setState}
              busy={busy}
              actions={(testing) =>
                saved ? (
                  <button
                    type="button"
                    className="btn btn-lg btn-accent"
                    onClick={carryOn}
                    disabled={busy || testing}
                  >
                    {going === "continue" && <span className="btn-spinner" aria-hidden="true" />}
                    {Icon.play} Continue build
                  </button>
                ) : null
              }
              secondary={
                <button type="button" className="btn btn-ghost" onClick={later} disabled={busy}>
                  Continue, I&apos;ll add it later
                </button>
              }
            />
          )}
        </div>
      </div>

      <p className="db-lock">
        {Icon.lock}
        <span>
          Encrypted on this computer. The crew sees only the{" "}
          {names.length === 1 ? "name" : "names"}{" "}
          {names.map((n, i) => (
            <span key={n}>
              {i > 0 && (i === names.length - 1 ? " and " : ", ")}
              <code className="db-var">{n}</code>
            </span>
          ))}
          , not the values.
        </span>
      </p>
    </section>
  );
}

function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="notice notice-bad" role="alert">
      {Icon.alert}
      <div className="notice-body">
        <span className="notice-title">Couldn&apos;t load what this database needs</span>
        <span className="notice-text">{message}</span>
        <div className="notice-actions">
          <button className="btn btn-sm btn-primary" onClick={onRetry}>
            {Icon.refresh} Try again
          </button>
        </div>
      </div>
    </div>
  );
}

// ── the form: guide on the left, fields on the right ─────────────────────────
type Outcome =
  | { kind: "idle" }
  | { kind: "saved"; check: DatabaseCheck; notices: DatabaseProblem[] }
  | { kind: "failed"; check: DatabaseCheck; notices: DatabaseProblem[] }
  | { kind: "invalid"; problems: DatabaseProblem[]; notices: DatabaseProblem[] }
  | { kind: "error"; message: string };

function DatabaseForm({
  id,
  state,
  onState,
  busy,
  actions,
  secondary,
}: {
  id: string;
  state: DatabaseState;
  onState: (s: DatabaseState) => void;
  busy: boolean;
  /** The primary action once something is saved (Continue build, at the gate). */
  actions?: (testing: boolean) => ReactNode;
  /** Always beside Test and save, so nobody is stuck. */
  secondary?: ReactNode;
}) {
  const contract = state.contract!;
  const [provider, setProvider] = useState(state.provider ?? contract.provider);
  const [values, setValues] = useState<Record<string, string>>({});
  const [shown, setShown] = useState<Record<string, boolean>>({});
  const [blur, setBlur] = useState<Record<string, { bad?: string; note?: string }>>({});
  const [testing, setTesting] = useState(false);
  const [outcome, setOutcome] = useState<Outcome>({ kind: "idle" });
  const [replacing, setReplacing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [flash, setFlash] = useState<number | null>(null);
  const base = useId();

  const guide = guideFor(contract.database, provider);
  const saved = state.saved ?? [];
  const hasSaved = saved.length > 0 && !replacing;
  const pasteObject = contract.paste_object;
  const fields: DatabaseVar[] = pasteObject
    ? [
        {
          name: "firebaseConfig",
          label: "firebaseConfig object",
          secret: false,
          required: true,
          placeholder: 'const firebaseConfig = {\n  apiKey: "AIza…",\n  authDomain: "…",\n  …\n};',
          kind: "object",
          help: "Paste the whole object. It is split into its six variables when saved.",
        },
      ]
    : contract.variables;

  // A provider switch changes which fields exist; what was typed for another does not carry.
  function switchProvider(p: string) {
    if (p === provider) return;
    setProvider(p);
    setValues({});
    setBlur({});
    setOutcome({ kind: "idle" });
    setReplacing(true);
    api.getDatabase(id, p).then(onState).catch(() => {});
  }
  const activeVars = fields;

  function problemFor(name: string): string | undefined {
    return outcome.kind === "invalid" ? outcome.problems.find((p) => p.name === name)?.message : undefined;
  }

  async function save() {
    // Checks the page can make first: nothing leaves for a string with <password> in it.
    const local: Record<string, { bad?: string; note?: string }> = {};
    for (const v of activeVars) local[v.name] = blurCheck(v, values[v.name] ?? "");
    setBlur(local);
    if (Object.values(local).some((c) => c.bad)) {
      setOutcome({ kind: "idle" });
      focusFirstProblem(local);
      return;
    }
    setTesting(true);
    setOutcome({ kind: "idle" });
    try {
      const sent = Object.fromEntries(
        Object.entries(values).filter(([, v]) => v.trim()),
      );
      const r = await api.checkDatabase(id, sent, provider);
      onState(r.state);
      if (r.status === "invalid") {
        setOutcome({ kind: "invalid", problems: r.problems ?? [], notices: r.notices ?? [] });
        const first = r.problems?.[0]?.name;
        if (first) document.getElementById(`${base}-${first}`)?.focus();
      } else if (r.ok && r.check) {
        setOutcome({ kind: "saved", check: r.check, notices: r.notices ?? [] });
        setValues({});
        setReplacing(false);
      } else if (r.check) {
        setOutcome({ kind: "failed", check: r.check, notices: r.notices ?? [] });
      }
    } catch (e: any) {
      const message =
        e instanceof ApiError && e.status === 429
          ? "Too many tests in a few minutes. Nothing was saved. Try again shortly."
          : e?.message || "The test didn't finish. Try again.";
      setOutcome({ kind: "error", message });
    } finally {
      setTesting(false);
    }
  }

  function focusFirstProblem(local: Record<string, { bad?: string }>) {
    const name = Object.keys(local).find((k) => local[k].bad);
    if (name) document.getElementById(`${base}-${name}`)?.focus();
  }

  async function remove() {
    setRemoving(true);
    try {
      onState(await api.removeDatabase(id));
      setOutcome({ kind: "idle" });
    } catch (e: any) {
      setOutcome({ kind: "error", message: e?.message || "Couldn't remove it. Try again." });
    } finally {
      setRemoving(false);
    }
  }

  function showStep(step: number | null | undefined) {
    if (!step) return;
    setFlash(step);
    document.getElementById(`${base}-step-${step}`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    window.setTimeout(() => setFlash(null), 1600);
  }

  const failedName =
    outcome.kind === "failed" ? outcome.check.name ?? activeVars[0]?.name : undefined;
  const notices =
    outcome.kind === "saved" || outcome.kind === "failed" || outcome.kind === "invalid"
      ? outcome.notices
      : [];

  return (
    <div className="db-form">
      {(state.providers?.length ?? 0) > 1 && (
        <div className="db-providers">
          <span className="label" id={`${base}-prov`}>Where is it hosted?</span>
          <div className="switcher" role="radiogroup" aria-labelledby={`${base}-prov`}>
            {state.providers!.map((p) => (
              <button
                key={p.provider}
                type="button"
                role="radio"
                className="seg-btn"
                aria-checked={provider === p.provider}
                aria-pressed={provider === p.provider}
                onClick={() => switchProvider(p.provider)}
                disabled={testing}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="db-grid">
        <div className="db-guide">
          <h3 className="db-guide-title">Where to find it · {guide.title}</h3>
          <ol className="db-steps">
            {guide.steps.map((s, i) => (
              <li
                key={i}
                id={`${base}-step-${i + 1}`}
                className={flash === i + 1 ? "db-step-flash" : undefined}
              >
                {s}
              </li>
            ))}
          </ol>
          {guide.open && (
            <a className="btn btn-sm" href={guide.open.href} target="_blank" rel="noopener noreferrer">
              {guide.open.label} {Icon.external}
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
              <div className="db-saved-actions">
                <button type="button" className="btn btn-sm" onClick={() => setReplacing(true)} disabled={busy || removing}>
                  {Icon.pen} Replace
                </button>
                <button type="button" className="btn btn-sm btn-danger" onClick={remove} disabled={busy || removing}>
                  {removing ? <span className="btn-spinner" aria-hidden="true" /> : Icon.trash} Remove
                </button>
              </div>
            </div>
          ) : (
            activeVars.map((v) => {
              const fieldId = `${base}-${v.name}`;
              const hintId = `${fieldId}-hint`;
              const bad = blur[v.name]?.bad || problemFor(v.name);
              const failedHere = failedName === v.name && outcome.kind === "failed";
              const note = blur[v.name]?.note || notices.find((n) => n.name === v.name)?.message;
              const secret = v.secret || v.kind === "uri";
              const masked = secret && !shown[v.name];
              const common = {
                id: fieldId,
                value: values[v.name] ?? "",
                placeholder: v.placeholder,
                autoComplete: "off",
                spellCheck: false,
                autoCapitalize: "off",
                "aria-describedby": hintId,
                "aria-invalid": Boolean(bad || failedHere) || undefined,
                disabled: testing,
                onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
                  setValues((prev) => ({ ...prev, [v.name]: e.target.value }));
                  if (blur[v.name]) setBlur((prev) => ({ ...prev, [v.name]: {} }));
                  // An edit answers the last failure; keeping it on screen would hide
                  // what the new value says about itself.
                  if (outcome.kind === "failed" || outcome.kind === "invalid") setOutcome({ kind: "idle" });
                },
                onBlur: () => setBlur((prev) => ({ ...prev, [v.name]: blurCheck(v, values[v.name] ?? "") })),
              };
              return (
                <div className="field db-field" key={v.name}>
                  <label htmlFor={fieldId}>
                    {v.label}
                    {!v.required && <span className="db-optional">optional</span>}
                    {v.kind !== "object" && <code className="db-var">{v.name}</code>}
                  </label>
                  {v.kind === "object" ? (
                    <textarea className="textarea input-mono db-input" rows={6} {...common} />
                  ) : (
                    <div className="db-input-row">
                      <input
                        className="input input-mono db-input"
                        type={masked ? "password" : "text"}
                        {...common}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") save();
                        }}
                      />
                      {secret && (
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
                        <span>{bad}</span>
                      </p>
                    ) : failedHere && outcome.kind === "failed" ? (
                      <p className="db-msg db-msg-bad" role="alert">
                        {Icon.alert}
                        <span>
                          {outcome.check.message}
                          {outcome.check.step ? (
                            <>
                              {" "}
                              <button type="button" className="db-steplink" onClick={() => showStep(outcome.check.step)}>
                                See step {outcome.check.step}
                              </button>
                            </>
                          ) : null}
                        </span>
                      </p>
                    ) : note ? (
                      <p className="db-msg db-msg-warn">
                        {Icon.info}
                        <span>{note}</span>
                      </p>
                    ) : v.help ? (
                      <p className="field-hint">{v.help}</p>
                    ) : null}
                  </div>
                </div>
              );
            })
          )}

          <div className="db-status" aria-live="polite">
            {testing ? (
              <p className="db-msg db-msg-run">
                <span className="btn-spinner" aria-hidden="true" />
                <span>Testing connection…</span>
              </p>
            ) : outcome.kind === "saved" && outcome.check.status === "connected" ? (
              <p className="db-msg db-msg-ok">
                {Icon.check}
                <span>
                  Connected to <code className="db-host">{outcome.check.host}</code>
                  {outcome.check.latency_ms != null && <> · {outcome.check.latency_ms} ms</>}
                </span>
              </p>
            ) : outcome.kind === "saved" ? (
              <p className="db-msg db-msg-warn">
                {Icon.alert}
                <span>{outcome.check.message}</span>
              </p>
            ) : outcome.kind === "error" ? (
              <p className="db-msg db-msg-bad" role="alert">
                {Icon.alert}
                <span>{outcome.message}</span>
              </p>
            ) : outcome.kind === "failed" && failedName === undefined ? (
              <p className="db-msg db-msg-bad" role="alert">
                {Icon.alert}
                <span>{outcome.check.message}</span>
              </p>
            ) : hasSaved && state.check ? (
              <StatusLine state={state} />
            ) : null}
          </div>

          {outcome.kind === "saved" && outcome.notices.length > 0 && (
            <ul className="db-notices">
              {outcome.notices.map((n, i) => (
                <li key={i} className="db-msg db-msg-warn">
                  {Icon.info}
                  <span>{n.message}</span>
                </li>
              ))}
            </ul>
          )}

          <div className="db-actions">
            {hasSaved ? (
              actions?.(testing)
            ) : (
              <button
                type="button"
                className="btn btn-lg btn-accent"
                onClick={save}
                disabled={busy || testing}
              >
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

function StatusLine({ state }: { state: DatabaseState }) {
  const c = state.check;
  if (!c) return null;
  if (state.status === "connected") {
    return (
      <p className="db-msg db-msg-ok">
        {Icon.check}
        <span>
          Connected to <code className="db-host">{c.host}</code>
          {c.latency_ms != null && <> · {c.latency_ms} ms</>}
        </span>
      </p>
    );
  }
  return (
    <p className="db-msg db-msg-warn">
      {Icon.alert}
      <span>Saved, but we couldn&apos;t sign in to test it from here. The crew builds against it anyway.</span>
    </p>
  );
}

// ── the project's Database section (Deliver tab) ─────────────────────────────
export function databaseBadge(status: string | null | undefined): { cls: string; text: string } {
  if (status === "connected") return { cls: "badge-ok", text: "Connected" };
  if (status === "unchecked") return { cls: "badge-warn", text: "Saved, not tested" };
  return { cls: "badge-warn", text: "Not connected" };
}

export function DatabasePanel({
  id,
  refreshKey,
  onChange,
}: {
  id: string;
  refreshKey?: unknown;
  onChange?: (s: DatabaseState) => void;
}) {
  const { state, setState, error, reload } = useDatabase(id, refreshKey);
  useEffect(() => {
    if (state) onChange?.(state);
  }, [state, onChange]);

  if (error) {
    return (
      <div className="card">
        <LoadError message={error} onRetry={reload} />
      </div>
    );
  }
  if (!state || !state.needed || !state.contract) return null;
  const badge = databaseBadge(state.status);

  return (
    <div className="card db-card" aria-labelledby="db-card-title">
      <div className="sec-head">
        <h2 className="label" id="db-card-title">Database</h2>
        <span className="rule" />
        <span className={`badge ${badge.cls}`}>
          <span className={`dot ${badge.cls === "badge-ok" ? "dot-ok" : "dot-warn"}`} aria-hidden="true" />
          {badge.text}
        </span>
      </div>
      <p className="muted db-card-lede">
        {state.database_label} was chosen by Atlas. The code reads it from{" "}
        {state.contract.variables.map((v, i) => (
          <span key={v.name}>
            {i > 0 && ", "}
            <code className="db-var">{v.name}</code>
          </span>
        ))}
        {state.status === "connected" || state.status === "unchecked"
          ? "."
          : ". Until you connect it, the download has placeholders in .env.example."}
      </p>
      <DatabaseForm id={id} state={state} onState={setState} busy={false} />
      <p className="db-lock db-lock-inline">
        {Icon.lock}
        <span>Encrypted on this computer. Only the host or last four characters are shown again.</span>
      </p>
    </div>
  );
}
