"use client";

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import {
  ApiError,
  api,
  type DeployFix,
  type DeployState,
  type GithubPushResult,
  type ShipInfo,
} from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";

/** Which flow the card has open. The completion banner can ask for one. */
export type ShipIntent = "deploy" | "github";

// Light client-side mirror of the backend slug() so the prefilled repo name
// matches what GitHub will actually get.
function slug(text: string): string {
  const s = (text || "project")
    .slice(0, 48)
    .replace(/[^a-zA-Z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
  return s || "project";
}

const REPO_NAME = /^[A-Za-z0-9._-]{1,100}$/;
const IN_FLIGHT = new Set(["queued", "uploading", "building"]);

// Monochrome brand marks (Simple Icons paths), drawn in currentColor.
const VercelMark = (
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="currentColor">
    <path d="m12 1.608 12 20.784H0Z" />
  </svg>
);
const RenderMark = (
  <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="currentColor">
    <path d="M18.263.007c-3.121-.147-5.744 2.109-6.192 5.082-.018.138-.045.272-.067.405-.696 3.703-3.936 6.507-7.827 6.507-1.388 0-2.691-.356-3.825-.979a.202.202 0 0 0-.302.178V24H12v-8.999c0-1.656 1.338-3 2.987-3h2.988c3.382 0 6.103-2.817 5.97-6.244-.12-3.084-2.61-5.603-5.682-5.75" />
  </svg>
);

function kindLabel(info: ShipInfo): string {
  const kind =
    info.kind === "frontend" ? "frontend only" : info.kind === "backend" ? "backend only" : "full stack";
  return info.stack ? `${kind} · ${info.stack}` : kind;
}

function ago(iso: string | null): string {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

function handoffUrl(repo: string, branch: string | null): string {
  let url = `https://github.com/${repo}`;
  if (branch && branch !== "main" && branch !== "master") url += `/tree/${encodeURIComponent(branch)}`;
  return `https://render.com/deploy?repo=${url}`;
}

export default function ShipCard({
  id,
  defaultName,
  intent,
  onIntentUsed,
  onShipped,
}: {
  id: string;
  defaultName: string;
  /** Asked for from the completion banner: open that flow once. */
  intent?: ShipIntent | null;
  onIntentUsed?: () => void;
  /** A push or deploy changed where this build lives — the header reloads. */
  onShipped?: () => void;
}) {
  const [info, setInfo] = useState<ShipInfo | null>(null);
  const [loadError, setLoadError] = useState("");
  const [open, setOpen] = useState<ShipIntent | null>(null);
  const [oauthNotice, setOauthNotice] = useState<"connected" | "error" | "">("");
  const cardRef = useRef<HTMLElement>(null);
  const panelId = useId();

  const refresh = useCallback(async () => {
    try {
      setInfo(await api.shipInfo(id));
      setLoadError("");
    } catch (e: any) {
      setLoadError(e?.message || "The backend didn't answer.");
    }
  }, [id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Back from GitHub's sign-in: say how it went, reopen the flow it started from
  // (`next`), and clean the URL so a reload doesn't say it again.
  useEffect(() => {
    const sp = new URLSearchParams(window.location.search);
    const g = sp.get("github");
    const next = sp.get("next");
    if (g === "connected" || g === "error") setOauthNotice(g);
    if (next === "deploy" || next === "github") setOpen(next);
    else if (g) setOpen("github");
    if (g || next) {
      sp.delete("github");
      sp.delete("next");
      const qs = sp.toString();
      window.history.replaceState({}, "", window.location.pathname + (qs ? `?${qs}` : "") + window.location.hash);
      requestAnimationFrame(() => cardRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    }
  }, []);

  useEffect(() => {
    if (!intent) return;
    setOpen(intent);
    onIntentUsed?.();
    requestAnimationFrame(() => cardRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }, [intent, onIntentUsed]);

  const changed = useCallback(async () => {
    await refresh();
    onShipped?.();
  }, [refresh, onShipped]);

  const toggle = (which: ShipIntent) => setOpen((cur) => (cur === which ? null : which));

  if (loadError && !info) {
    return (
      <section className="card" ref={cardRef}>
        <div className="sec-head">
          <h2 className="label">Ship it</h2>
          <span className="rule" />
        </div>
        <div className="notice notice-bad" role="alert">
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Deploy options didn&apos;t load</span>
            <span className="notice-text">
              The backend didn&apos;t answer. Check that it&apos;s running on <code>:8000</code>. The
              .zip above still works.
            </span>
            <span className="notice-detail mono">{loadError}</span>
            <div className="notice-actions">
              <button className="btn btn-sm" onClick={refresh}>
                {Icon.refresh} Try again
              </button>
            </div>
          </div>
        </div>
      </section>
    );
  }

  if (!info) {
    return (
      <section className="card" ref={cardRef}>
        <SkeletonLines lines={3} />
      </section>
    );
  }

  const vercelTarget = info.target === "vercel";
  const deploySub = !info.kind
    ? "Nothing to deploy yet"
    : vercelTarget
      ? "To Vercel · live URL in about a minute"
      : info.kind === "backend"
        ? "GitHub → Render · API and database"
        : "GitHub → Render · frontend, API and database";

  return (
    <section className="card ship" ref={cardRef} aria-labelledby={`${panelId}-title`}>
      <div className="sec-head">
        <h2 className="label" id={`${panelId}-title`}>
          Ship it
        </h2>
        <span className="rule" />
        {info.kind && <span className="badge badge-mono">{kindLabel(info)}</span>}
      </div>
      <p className="ship-lead">
        Put it online in your own {vercelTarget ? "Vercel" : "Render"} account, or keep the code in your GitHub.
      </p>

      {info.deploy.url && (
        <p className="ship-live">
          <span className="dot dot-ok" aria-hidden="true" />
          Live at{" "}
          <a className="link mono" href={info.deploy.url} target="_blank" rel="noreferrer">
            {info.deploy.url.replace(/^https:\/\//, "")}
          </a>
        </p>
      )}

      <div className="ship-tiles">
        <button
          type="button"
          className="ship-tile ship-tile-accent"
          aria-expanded={open === "deploy"}
          aria-controls={`${panelId}-deploy`}
          onClick={() => toggle("deploy")}
          disabled={!info.target}
        >
          <span className="ship-tile-mark">{vercelTarget ? VercelMark : RenderMark}</span>
          <span className="ship-tile-text">
            <span className="ship-tile-title">Deploy it</span>
            <span className="ship-tile-sub">{deploySub}</span>
          </span>
          <span className="ship-tile-chev" aria-hidden="true">
            {Icon.chevron}
          </span>
        </button>
        <button
          type="button"
          className="ship-tile"
          aria-expanded={open === "github"}
          aria-controls={`${panelId}-github`}
          onClick={() => toggle("github")}
        >
          <span className="ship-tile-mark">{Icon.github}</span>
          <span className="ship-tile-text">
            <span className="ship-tile-title">{info.github_repo ? "GitHub" : "Connect to GitHub"}</span>
            <span className="ship-tile-sub">
              {info.github_repo ? info.github_repo : "Push to a private repo in your account"}
            </span>
          </span>
          <span className="ship-tile-chev" aria-hidden="true">
            {Icon.chevron}
          </span>
        </button>
      </div>

      <div className="ship-drawer" data-open={open === "deploy"} id={`${panelId}-deploy`}>
        <div className="ship-drawer-inner">
          {open === "deploy" &&
            (vercelTarget ? (
              <VercelFlow id={id} info={info} onChange={changed} />
            ) : (
              <RenderFlow id={id} info={info} defaultName={defaultName} oauthNotice={oauthNotice} onChange={changed} />
            ))}
        </div>
      </div>
      <div className="ship-drawer" data-open={open === "github"} id={`${panelId}-github`}>
        <div className="ship-drawer-inner">
          {open === "github" && (
            <GithubFlow id={id} info={info} defaultName={defaultName} oauthNotice={oauthNotice} onChange={changed} />
          )}
        </div>
      </div>

      {!info.ready && info.kind && (
        <p className="field-hint ship-foot">Deploy is ready once the build is complete. GitHub takes any output.</p>
      )}
      {info.version != null && (
        // Which version goes out (#79) — and when the one live is older.
        <p className="field-hint ship-foot ship-version">
          Deploy and GitHub send <b className="mono">v{info.version}</b>.
          {info.deployed_version && info.deployed_version !== info.version
            ? ` v${info.deployed_version} is the one deployed now.`
            : ""}
          {info.pushed_version && info.pushed_version !== info.version
            ? ` GitHub has v${info.pushed_version}.`
            : ""}
        </p>
      )}
      <p className="ship-trust">
        {Icon.lock}
        <span>Your tokens stay encrypted on this server. Nothing is hosted on our account.</span>
      </p>
    </section>
  );
}

// ── shared bits ─────────────────────────────────────────────────────────────
function Problem({ title, children, actions }: { title: string; children?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="notice notice-bad" role="alert">
      {Icon.alert}
      <div className="notice-body">
        <span className="notice-title">{title}</span>
        {children && <span className="notice-text">{children}</span>}
        {actions && <div className="notice-actions">{actions}</div>}
      </div>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-sm"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1600);
        } catch {
          setDone(false);
        }
      }}
    >
      {done ? Icon.check : Icon.copy} {done ? "Copied" : "Copy"}
    </button>
  );
}

function connectGithub(next: ShipIntent) {
  const back = `${window.location.origin}${window.location.pathname}?next=${next}`;
  window.location.href = api.githubConnectUrl(back);
}

function GithubConnect({
  info,
  next,
  lead,
  oauthNotice,
}: {
  info: ShipInfo;
  next: ShipIntent;
  lead: string;
  oauthNotice: string;
}) {
  const gh = info.connections.github;
  if (!gh.configured) {
    return (
      <p className="ship-copy">
        GitHub isn&apos;t enabled on this server yet. The operator needs to add a free{" "}
        <a className="link" href="https://github.com/settings/developers" target="_blank" rel="noreferrer">
          GitHub OAuth App
        </a>{" "}
        and set <code>GITHUB_CLIENT_ID</code> and <code>GITHUB_CLIENT_SECRET</code> in the backend&apos;s{" "}
        <code>.env</code>.
      </p>
    );
  }
  return (
    <div className="ship-stack">
      {gh.reason === "revoked" ? (
        <Problem title="GitHub access was removed">
          GitHub rejected the saved connection. Connect again to keep pushing.
        </Problem>
      ) : (
        <p className="ship-copy">{lead}</p>
      )}
      {oauthNotice === "error" && (
        <Problem title="The GitHub connection didn't complete">
          It was cancelled, or GitHub didn&apos;t answer. Connect again.
        </Problem>
      )}
      <div>
        <button type="button" className="btn btn-primary" onClick={() => connectGithub(next)}>
          {Icon.github} Connect GitHub
        </button>
      </div>
    </div>
  );
}

/** Repo name + Private, and the answer to a name that's already taken. */
function RepoForm({
  defaultName,
  busy,
  action,
  onSubmit,
  conflict,
  onUseExisting,
}: {
  defaultName: string;
  busy: boolean;
  action: string;
  onSubmit: (name: string, priv: boolean) => void;
  conflict: { full_name: string; usable: boolean } | null;
  onUseExisting: (name: string) => void;
}) {
  const [name, setName] = useState(slug(defaultName));
  const [priv, setPriv] = useState(true);
  const fieldId = useId();
  const valid = REPO_NAME.test(name.trim());
  return (
    <div className="ship-stack">
      <form
        className="ship-repo"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid && !busy) onSubmit(name.trim(), priv);
        }}
      >
        <div className="field ship-repo-name">
          <label htmlFor={fieldId}>Repository name</label>
          <input
            id={fieldId}
            className="input input-mono"
            value={name}
            onChange={(e) => setName(e.target.value)}
            spellCheck={false}
            autoComplete="off"
            aria-invalid={!valid}
            aria-describedby={`${fieldId}-hint`}
            disabled={busy}
          />
        </div>
        <button
          type="button"
          className="switch ship-switch"
          role="switch"
          aria-checked={priv}
          onClick={() => setPriv((v) => !v)}
          disabled={busy}
        >
          <span className="switch-track" aria-hidden="true" />
          Private
        </button>
        <button type="submit" className="btn btn-primary ship-go" disabled={busy || !valid}>
          {busy && <span className="btn-spinner" aria-hidden="true" />}
          {busy ? "Pushing…" : action}
        </button>
      </form>
      <p className={`field-hint${valid ? "" : " ship-hint-bad"}`} id={`${fieldId}-hint`}>
        {valid ? "Letters, digits, - _ and . only." : "Use letters, digits, - _ and . with no spaces."}
      </p>
      {conflict && (
        <Problem
          title={`You already have ${conflict.full_name}`}
          actions={
            conflict.usable ? (
              <button type="button" className="btn btn-sm" onClick={() => onUseExisting(name.trim())} disabled={busy}>
                Use existing repo
              </button>
            ) : undefined
          }
        >
          {conflict.usable
            ? "It's empty, so this project can go into it. Or pick another name above."
            : "It already has other code in it. Pick another name above."}
        </Problem>
      )}
    </div>
  );
}

// ── Vercel ──────────────────────────────────────────────────────────────────
export function VercelConnect({ onSaved }: { onSaved: () => void }) {
  const [token, setToken] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fieldId = useId();

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await api.saveVercelToken(token.trim());
      if (r.applied) {
        setToken("");
        onSaved();
      } else {
        setError(r.message || "Vercel didn't accept that token.");
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="ship-stack" onSubmit={save}>
      <p className="ship-copy">
        Connect Vercel once. We deploy with a token from <b>your</b> account, so the site is yours.
      </p>
      <ol className="ship-guide">
        <li>
          Open <b>vercel.com → Account Settings → Tokens</b>
        </li>
        <li>
          <b>Create</b> a token, scoped to your personal account
        </li>
        <li>Copy it and paste it here</li>
      </ol>
      <div>
        <a className="btn btn-sm" href="https://vercel.com/account/tokens" target="_blank" rel="noreferrer">
          Open Vercel tokens {Icon.external}
        </a>
      </div>
      <div className="ship-token">
        <div className="field ship-token-field">
          <label htmlFor={fieldId}>Vercel token</label>
          <div className="ship-secret">
            <input
              id={fieldId}
              className="input input-mono"
              type={show ? "text" : "password"}
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder="Paste the token"
              disabled={busy}
              aria-invalid={Boolean(error)}
              aria-describedby={error ? `${fieldId}-err` : undefined}
            />
            <button
              type="button"
              className="icon-btn ship-reveal"
              onClick={() => setShow((v) => !v)}
              aria-label={show ? "Hide token" : "Show token"}
              aria-pressed={show}
            >
              {show ? Icon.eyeOff : Icon.eye}
            </button>
          </div>
        </div>
        <button type="submit" className="btn btn-primary ship-go" disabled={busy || !token.trim()}>
          {busy && <span className="btn-spinner" aria-hidden="true" />}
          {busy ? "Checking…" : "Check and save"}
        </button>
      </div>
      {error && (
        <p className="ship-error" id={`${fieldId}-err`} role="alert">
          {Icon.alert} {error}
        </p>
      )}
    </form>
  );
}

type StepState = "todo" | "run" | "done" | "bad";

function Steps({ steps }: { steps: { label: string; state: StepState; time?: string }[] }) {
  return (
    <ol className="ship-steps">
      {steps.map((s) => (
        <li key={s.label} className="ship-step" data-state={s.state}>
          <span className="ship-step-mark" aria-hidden="true">
            {s.state === "run" ? (
              <span className="btn-spinner" />
            ) : s.state === "done" ? (
              Icon.check
            ) : s.state === "bad" ? (
              Icon.alert
            ) : (
              <span className="dot" />
            )}
          </span>
          <span className="ship-step-label">{s.label}</span>
          {s.time && <span className="ship-step-time mono">{s.time}</span>}
          <span className="sr-only">
            {s.state === "run" ? " — in progress" : s.state === "done" ? " — done" : s.state === "bad" ? " — failed" : ""}
          </span>
        </li>
      ))}
    </ol>
  );
}

function VercelFlow({ id, info, onChange }: { id: string; info: ShipInfo; onChange: () => Promise<void> }) {
  const vc = info.connections.vercel;
  const [state, setState] = useState<DeployState>(info.deploy);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [removing, setRemoving] = useState(false);
  const [replacing, setReplacing] = useState(false);
  // When each step was first seen, for the elapsed time beside it.
  const seen = useRef<Record<string, number>>({});
  const [, tick] = useState(0);

  useEffect(() => setState(info.deploy), [info.deploy]);

  const mine = state.target === "vercel";
  const status = mine ? state.status : null;
  const live = status && IN_FLIGHT.has(status);

  useEffect(() => {
    if (status && !seen.current[status]) seen.current[status] = Date.now();
  }, [status]);

  // One poll at a time: the next is scheduled when the last one answers, so a slow
  // answer (Vercel's log, read once a build fails) is never overtaken by another.
  useEffect(() => {
    if (!live) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      tick((n) => n + 1);
      try {
        const next = await api.deployState(id);
        if (stop) return;
        setState(next);
        // `fixing`: Vercel failed it and the crew has it now (#75) — the build is
        // running again, so the page around this card reloads too.
        if (next.status === "ready" || next.status === "error" || next.status === "fixing") onChange();
      } catch {
        // The next poll tries again.
      }
      if (!stop) timer = setTimeout(poll, 2500);
    };
    timer = setTimeout(poll, 2500);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [live, id, onChange]);

  // While the crew fixes what Vercel rejected, follow its rounds — slower than a
  // deploy, since a round is minutes — until it finishes or asks for help.
  const fixing = status === "fixing";
  const crewAtWork = fixing && (!state.fix || state.fix.state === "fixing");
  useEffect(() => {
    if (!crewAtWork) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const next = await api.deployState(id);
        if (stop) return;
        setState(next);
        if (next.status !== "fixing" || next.fix?.state !== "fixing") {
          onChange();
          return;
        }
      } catch {
        // The next poll tries again.
      }
      if (!stop) timer = setTimeout(poll, 4000);
    };
    timer = setTimeout(poll, 4000);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [crewAtWork, id, onChange]);

  async function start() {
    setBusy(true);
    setError("");
    seen.current = {};
    try {
      const r = await api.deploy(id);
      setState(r.deploy);
    } catch (e: any) {
      if (e instanceof ApiError && e.data?.needs === "vercel") await onChange();
      else setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setRemoving(true);
    try {
      await api.removeVercelToken();
      await onChange();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setRemoving(false);
    }
  }

  if (!vc.connected || replacing) {
    return (
      <div className="ship-stack">
        {replacing && (
          <button type="button" className="btn btn-sm btn-ghost ship-back" onClick={() => setReplacing(false)}>
            Keep {vc.hint}
          </button>
        )}
        {mine && state.status === "error" && state.error?.includes("rejected") && !replacing && (
          <Problem title="Token rejected">{state.error}</Problem>
        )}
        <VercelConnect
          onSaved={async () => {
            setReplacing(false);
            await onChange();
          }}
        />
      </div>
    );
  }

  const since = (key: string, until?: string) => {
    const a = seen.current[key];
    if (!a) return undefined;
    const b = until && seen.current[until] ? seen.current[until] : Date.now();
    return `${Math.max(0, Math.round((b - a) / 1000))}s`;
  };
  // A build the crew is fixing (or fixed) failed on Vercel's side, after the upload.
  const sentBack = status === "fixing" || status === "fixed";
  const failedAt = sentBack
    ? "Building"
    : status === "error"
      ? seen.current.building || state.log.length > 0
        ? "Building"
        : "Uploading files"
      : null;
  const steps: { label: string; state: StepState; time?: string }[] = [
    {
      label: "Uploading files",
      state:
        status === "queued" || status === "uploading"
          ? "run"
          : failedAt === "Uploading files"
            ? "bad"
            : status
              ? "done"
              : "todo",
      time: since(seen.current.uploading ? "uploading" : "queued", "building"),
    },
    {
      label: "Building on Vercel",
      state: status === "building" ? "run" : failedAt === "Building" ? "bad" : status === "ready" ? "done" : "todo",
      time: since("building", "ready"),
    },
    { label: "Live", state: status === "ready" ? "done" : "todo" },
  ];

  return (
    <div className="ship-stack">
      <div className="ship-conn">
        <span className="ship-conn-mark">{VercelMark}</span>
        <span className="ship-conn-text">
          Connected as <b>{vc.username}</b> · <span className="mono">{vc.hint}</span>
        </span>
        <span className="ship-conn-actions">
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => setReplacing(true)} disabled={Boolean(live)}>
            Replace
          </button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={remove} disabled={removing || Boolean(live)}>
            Remove
          </button>
        </span>
      </div>

      {status && status !== "fixed" && (
        <div aria-live="polite">
          <Steps steps={steps} />
        </div>
      )}

      {sentBack && state.fix && <CrewFix fix={state.fix} />}

      {status === "ready" && state.url && (
        <div className="notice notice-ok">
          {Icon.check}
          <div className="notice-body">
            <span className="notice-title">It&apos;s live</span>
            <a className="link mono ship-url" href={state.url} target="_blank" rel="noreferrer">
              {state.url} {Icon.external}
            </a>
            <div className="notice-actions">
              <CopyButton text={state.url} />
              <a className="btn btn-sm" href="https://vercel.com/dashboard" target="_blank" rel="noreferrer">
                Open in Vercel {Icon.external}
              </a>
            </div>
          </div>
        </div>
      )}

      {status === "error" && (
        <Problem title={state.error?.startsWith("Build failed") ? "Build failed — see log" : "The deploy didn't finish"}>
          {state.error}
        </Problem>
      )}
      {(status === "error" || sentBack) && state.log.length > 0 && (
        <details className="ship-log">
          <summary>
            {sentBack ? "What Vercel said" : "Build log"} · last {state.log.length} lines
          </summary>
          <pre className="mono" tabIndex={0}>
            {state.log.join("\n")}
          </pre>
        </details>
      )}
      {error && <Problem title="Couldn't start the deploy">{error}</Problem>}

      <div className="ship-actions">
        <button
          type="button"
          className="btn btn-primary ship-go"
          onClick={start}
          disabled={busy || Boolean(live) || fixing || !info.ready}
        >
          {(busy || live) && <span className="btn-spinner" aria-hidden="true" />}
          {live
            ? "Deploying…"
            : status === "ready"
              ? info.version && info.deployed_version && info.deployed_version !== info.version
                ? `Deploy v${info.version}`
                : "Redeploy"
              : status === "fixed"
                ? "Deploy again"
                : fixing
                  ? "Deploy again once it's fixed"
                  : status === "error"
                    ? "Retry"
                    : "Deploy to Vercel"}
        </button>
        {!status && (
          <span className="field-hint">Uploads the frontend straight to your Vercel. No GitHub needed.</span>
        )}
      </div>
    </div>
  );
}

/**
 * Vercel failed the build, and the crew has it (#75): which round, of how many, and
 * how it ended. No decision is needed while it works — the person reads progress, then
 * deploys again once the build has finished on the fixed code.
 */
function CrewFix({ fix }: { fix: DeployFix }) {
  const who = AGENT_BY_KEY.frontend_engineer;
  const name = who ? `${who.codename}, the ${who.role}` : "the Frontend Engineer";
  const round = Math.max(fix.round, 1);
  const of = Math.max(fix.of, round);
  const errors = fix.problems === 1 ? "error" : `${fix.problems} errors`;

  if (fix.state === "fixed") {
    return (
      <div className={`notice ${fix.verified ? "notice-ok" : "notice-warn"} ship-fix`} role="status">
        {fix.verified ? Icon.check : Icon.info}
        <div className="notice-body">
          <span className="notice-title">
            {fix.verified ? "The crew fixed what Vercel rejected" : "The crew rebuilt what Vercel rejected"}
          </span>
          <span className="notice-text">
            {fix.verified
              ? `The frontend was rebuilt from Vercel's ${errors} and built cleanly here. Deploy again to put it live.`
              : `The frontend was rebuilt from Vercel's ${errors}, but it couldn't be built here to check. Deploy again to see if Vercel takes it.`}
          </span>
        </div>
      </div>
    );
  }
  if (fix.state === "stuck") {
    return (
      <div className="notice notice-bad ship-fix" role="status">
        {Icon.alert}
        <div className="notice-body">
          <span className="notice-title">The crew couldn&apos;t fix it on its own</span>
          <span className="notice-text">
            It stopped after {round} round{round === 1 ? "" : "s"}. Open the Build tab to keep trying or decide what to do.
          </span>
        </div>
      </div>
    );
  }
  if (fix.state === "review") {
    return (
      <div className="notice notice-warn ship-fix" role="status">
        {Icon.check}
        <div className="notice-body">
          <span className="notice-title">Fixed. It&apos;s waiting for your review</span>
          <span className="notice-text">
            Approve it on the Build tab. Then deploy again.
          </span>
        </div>
      </div>
    );
  }
  if (fix.state === "stopped") {
    return (
      <div className="notice ship-fix" role="status">
        {Icon.stop}
        <div className="notice-body">
          <span className="notice-title">The fix was stopped</span>
          <span className="notice-text">Resume the build on the Build tab to carry on.</span>
        </div>
      </div>
    );
  }
  return (
    <div className="notice notice-run ship-fix" role="status">
      <span className="ship-fix-spin" aria-hidden="true">
        {Icon.rotate}
      </span>
      <div className="notice-body">
        <span className="notice-title">
          The crew is fixing it (round {round} of {of})
        </span>
        <span className="notice-text">
          Vercel&apos;s {errors} went back to {name}. The steps after it rebuild on the fix.
        </span>
      </div>
      <span className="badge badge-run ship-fix-badge">
        <span className="dot dot-run dot-pulse" aria-hidden="true" />
        No decision needed
      </span>
    </div>
  );
}

// ── GitHub → Render ─────────────────────────────────────────────────────────
function RenderFlow({
  id,
  info,
  defaultName,
  oauthNotice,
  onChange,
}: {
  id: string;
  info: ShipInfo;
  defaultName: string;
  oauthNotice: string;
  onChange: () => Promise<void>;
}) {
  const gh = info.connections.github;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState<{ full_name: string; usable: boolean } | null>(null);
  const [handoff, setHandoff] = useState<string | null>(null);
  const [liveUrl, setLiveUrl] = useState(info.deploy.target === "render" ? info.deploy.url || "" : "");
  const [savedUrl, setSavedUrl] = useState("");
  const [urlError, setUrlError] = useState("");
  const urlId = useId();

  const step = !gh.connected ? 1 : !info.github_repo ? 2 : 3;
  const handedOff = info.deploy.target === "render" && info.deploy.status === "handed_off";

  // Step 2: only the push. Step 3 is the hand-off.
  async function push(body: { name: string; private?: boolean; use_existing?: boolean }) {
    setBusy(true);
    setError("");
    setConflict(null);
    try {
      await api.pushToGithub(id, body);
      await onChange();
    } catch (e: any) {
      if (e instanceof ApiError && e.data?.conflict) setConflict(e.data.conflict);
      else if (e instanceof ApiError && e.data?.needs) await onChange();
      else setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  // Step 3: push the latest build (no commit when nothing changed), then open
  // Render's Blueprint page. The window is opened inside the click, so no pop-up
  // blocker stops it; it is pointed at Render once the push has landed.
  async function openRender() {
    setBusy(true);
    setError("");
    const win = window.open("about:blank", "_blank");
    try {
      const r = await api.deploy(id);
      const url = r.handoff_url || null;
      setHandoff(url);
      if (url && win) {
        win.opener = null;
        win.location.href = url;
      } else win?.close();
      await onChange();
    } catch (e: any) {
      win?.close();
      if (e instanceof ApiError && e.data?.needs) await onChange();
      else setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveUrl(e: React.FormEvent) {
    e.preventDefault();
    setUrlError("");
    try {
      const r = await api.setLiveUrl(id, liveUrl);
      setLiveUrl(r.url || "");
      setSavedUrl(r.url || "");
      await onChange();
    } catch (err: any) {
      setUrlError(err.message);
    }
  }

  const stepState = (n: number) => (step > n ? "done" : step === n ? "current" : "todo");
  const openUrl = handoff || (info.github_repo ? handoffUrl(info.github_repo, info.github_branch) : null);

  return (
    <div className="ship-stack">
      <ol className="ship-stepper" aria-label="Deploy steps">
        {["GitHub", "Push", "Render"].map((label, i) => (
          <li key={label} data-state={stepState(i + 1)} aria-current={step === i + 1 ? "step" : undefined}>
            <span className="ship-stepper-n" aria-hidden="true">
              {step > i + 1 ? Icon.check : i + 1}
            </span>
            {label}
          </li>
        ))}
      </ol>

      {step === 1 && (
        <GithubConnect
          info={info}
          next="deploy"
          oauthNotice={oauthNotice}
          lead="Full-stack apps deploy from a GitHub repo. Connect GitHub to continue."
        />
      )}

      {step === 2 && (
        <>
          <p className="ship-copy">
            Pushing to <b>@{gh.login}</b>. The repo gets the code and a <code>render.yaml</code> that tells
            Render what to create. It holds variable names, not values.
          </p>
          <RepoForm
            defaultName={defaultName}
            busy={busy}
            action="Push"
            conflict={conflict}
            onSubmit={(name, priv) => push({ name, private: priv })}
            onUseExisting={(name) => push({ name, use_existing: true })}
          />
        </>
      )}

      {step === 3 && info.github_repo && (
        <>
          <p className="ship-repo-line">
            {Icon.github}
            <a className="link mono" href={`https://github.com/${info.github_repo}`} target="_blank" rel="noreferrer">
              {info.github_repo}
            </a>
            {info.github_pushed_at && <span className="dim">· pushed {ago(info.github_pushed_at)}</span>}
          </p>
          <ul className="ship-checklist">
            <li>Free web services sleep after 15 min idle; the first visit then takes 30–60 s.</li>
            {info.render?.free_postgres && <li>Free Postgres is deleted after 30 days, and there&apos;s one per workspace.</li>}
            <li>If the repo is private, Render will ask to access it.</li>
            {info.render && info.render.asks_for.length > 0 && (
              <li>
                Render will ask you for{" "}
                {info.render.asks_for.map((n, i) => (
                  <span key={n}>
                    {i > 0 && ", "}
                    <code>{n}</code>
                  </span>
                ))}{" "}
                on its own page. Paste the values there.
              </li>
            )}
          </ul>
          <div className="ship-actions">
            <button type="button" className="btn btn-primary ship-go" onClick={openRender} disabled={busy || !info.ready}>
              {busy && <span className="btn-spinner" aria-hidden="true" />}
              {busy ? "Pushing the latest…" : "Open Render to deploy"} {!busy && Icon.external}
            </button>
            <span className="field-hint">Pushes the latest build first, then opens Render in a new tab.</span>
          </div>
          {handoff && openUrl && (
            <p className="field-hint">
              Render didn&apos;t open?{" "}
              <a className="link" href={openUrl} target="_blank" rel="noopener noreferrer">
                Open it here {Icon.external}
              </a>
            </p>
          )}
        </>
      )}

      {error && <Problem title="That didn't go through">{error}</Problem>}

      {(handedOff || handoff) && step === 3 && (
        <form className="ship-live-form" onSubmit={saveUrl}>
          <div className="field">
            <label htmlFor={urlId}>Finish on Render. Paste your app URL here to keep it with the project</label>
            <div className="ship-repo">
              <input
                id={urlId}
                className="input input-mono ship-repo-name"
                value={liveUrl}
                onChange={(e) => setLiveUrl(e.target.value)}
                placeholder="https://your-app.onrender.com"
                spellCheck={false}
                autoComplete="off"
                inputMode="url"
                aria-invalid={Boolean(urlError)}
              />
              <button type="submit" className="btn ship-go">
                Save
              </button>
            </div>
          </div>
          {urlError && (
            <p className="ship-error" role="alert">
              {Icon.alert} {urlError}
            </p>
          )}
          {savedUrl && !urlError && (
            <p className="field-hint" role="status">
              Saved. It now shows on this build.
            </p>
          )}
        </form>
      )}
    </div>
  );
}

// ── GitHub on its own ───────────────────────────────────────────────────────
function GithubFlow({
  id,
  info,
  defaultName,
  oauthNotice,
  onChange,
}: {
  id: string;
  info: ShipInfo;
  defaultName: string;
  oauthNotice: string;
  onChange: () => Promise<void>;
}) {
  const gh = info.connections.github;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState<{ full_name: string; usable: boolean } | null>(null);
  const [result, setResult] = useState<GithubPushResult | null>(null);

  async function push(body: { name?: string; private?: boolean; use_existing?: boolean }) {
    setBusy(true);
    setError("");
    setConflict(null);
    setResult(null);
    try {
      setResult(await api.pushToGithub(id, body));
      await onChange();
    } catch (e: any) {
      if (e instanceof ApiError && e.data?.conflict) setConflict(e.data.conflict);
      else {
        setError(e.message);
        if (e instanceof ApiError && e.status === 409) await onChange();
      }
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    setError("");
    try {
      await api.githubDisconnect();
      setResult(null);
      await onChange();
    } catch (e: any) {
      setError(e.message);
    }
  }

  if (!gh.connected) {
    return (
      <GithubConnect
        info={info}
        next="github"
        oauthNotice={oauthNotice}
        lead="Sign in with GitHub and we'll push this project's source, docs and README to a private repo."
      />
    );
  }

  return (
    <div className="ship-stack">
      <div className="ship-conn">
        {gh.avatar ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img className="ship-avatar" src={gh.avatar} alt="" width={24} height={24} />
        ) : (
          <span className="ship-conn-mark">{Icon.github}</span>
        )}
        <span className="ship-conn-text">
          <b>@{gh.login}</b>
          {oauthNotice === "connected" && <span className="badge badge-ok">Connected</span>}
        </span>
        <span className="ship-conn-actions">
          <button type="button" className="btn btn-sm btn-ghost" onClick={disconnect} disabled={busy}>
            Disconnect
          </button>
        </span>
      </div>

      {info.github_repo ? (
        <>
          <p className="ship-repo-line">
            {Icon.github}
            <a className="link mono" href={`https://github.com/${info.github_repo}`} target="_blank" rel="noreferrer">
              github.com/{info.github_repo}
            </a>
            {Icon.external}
            {info.github_pushed_at && <span className="dim">· last push {ago(info.github_pushed_at)}</span>}
          </p>
          <div className="ship-actions">
            <button type="button" className="btn btn-primary ship-go" onClick={() => push({})} disabled={busy}>
              {busy && <span className="btn-spinner" aria-hidden="true" />}
              {busy ? "Pushing…" : "Push update"}
            </button>
            <span className="field-hint">Adds one commit with the current build. Files you added on GitHub stay.</span>
          </div>
        </>
      ) : (
        <RepoForm
          defaultName={defaultName}
          busy={busy}
          action="Push to GitHub"
          conflict={conflict}
          onSubmit={(name, priv) => push({ name, private: priv })}
          onUseExisting={(name) => push({ name, use_existing: true })}
        />
      )}

      {result && (
        <div className="notice notice-ok" role="status">
          {Icon.check}
          <div className="notice-body">
            <span className="notice-title">
              {result.changed === false
                ? "Already up to date"
                : result.created
                  ? `Pushed ${result.files} files`
                  : "Pushed the update"}
            </span>
            <span className="notice-text">
              <a className="link mono" href={result.html_url} target="_blank" rel="noreferrer">
                {result.full_name}
              </a>{" "}
              · {result.private ? "private" : "public"} · branch <code>{result.branch}</code>
            </span>
          </div>
        </div>
      )}
      {error && <Problem title="That push didn't go through">{error}</Problem>}
    </div>
  );
}
