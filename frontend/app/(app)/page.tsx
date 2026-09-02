"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, type LocalStatus, type RouterStatus } from "@/lib/api";
import { EXAMPLES } from "@/components/shell/phases";
import { AGENTS } from "@/components/agents/personas";
import AgentSprite from "@/components/agents/AgentSprite";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import RunSettings, {
  type RunConfig,
  DEFAULT_RUN_CONFIG,
  modelOptions,
  runtimeBlocker,
  settingsSummary,
} from "@/components/build/RunSettings";

/**
 * The home view: describe an idea, meet the crew that will build it.
 *
 * The idea is the only thing asked for up front. Routing and review policy used to
 * be answered *before* the idea had been typed — two segmented controls about model
 * economics standing between a person and the sentence they came here to write —
 * and they are now one summary line with the details behind a disclosure.
 */
export default function NewBuildPage() {
  const router = useRouter();
  useChrome({ sub: "New build" }, []);

  const [idea, setIdea] = useState("");
  const [config, setConfig] = useState<RunConfig>(DEFAULT_RUN_CONFIG);
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [createdId, setCreatedId] = useState("");

  // ── runtime preflight ────────────────────────────────────────────────────
  // Start build used to be primary and enabled with nothing behind it to run on:
  // the project was created, `run()` failed, its rejection was thrown away, and you
  // landed on a page whose state depended on which write won. The sidebar already
  // knew the answer; the composer never asked. Now it asks first.
  const [local, setLocal] = useState<LocalStatus | null>(null);
  const [models, setModels] = useState<RouterStatus | null>(null);
  const [checking, setChecking] = useState(true);

  const probe = useCallback(async () => {
    setChecking(true);
    const [localStatus, routerStatus] = await Promise.all([
      api.getLocalModel().catch(() => null),
      api.routerStatus().catch(() => null),
    ]);
    setLocal(localStatus);
    setModels(routerStatus);
    setChecking(false);
  }, []);

  useEffect(() => {
    probe();
  }, [probe]);

  const options = useMemo(() => modelOptions(local, models), [local, models]);
  const blocker = runtimeBlocker(config, local, models);

  async function start() {
    const trimmed = idea.trim();
    if (!trimmed || busy || blocker) return;
    setBusy(true);
    setError("");
    setCreatedId("");
    try {
      const project = await api.createProject({
        idea: trimmed,
        routing_mode: config.routing.backend,
        preferred_model: config.model || undefined,
        approval_mode: config.approval,
        cost_cap_usd: config.costCap ?? undefined,
      });
      setCreatedId(project.id);
      // Awaited, not fire-and-forget: a rejected run is the whole reason a build
      // used to open on a page that could not explain itself.
      await api.run(project.id);
      router.push(`/projects/${project.id}`);
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  }

  return (
    <div className="composer-page">
      <h1 className="composer-h1">
        What should we
        <br />
        <em>build today?</em>
      </h1>
      <p className="prose-lede composer-lede">
        Describe a product idea. Eight specialists take it from requirements through
        architecture, code, tests, security and deployment — and stop for you at the two
        moments your answer changes what they build.
      </p>

      <div className="composer">
        <label htmlFor="idea" className="sr-only">
          Product idea
        </label>
        <textarea
          id="idea"
          autoFocus
          placeholder="e.g. a habit-tracking app with streaks and smart reminders…"
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) start();
          }}
        />
        <div className="composer-foot">
          {/* The settings read as a sentence, and open only if you disagree with it. */}
          <button
            type="button"
            className="run-settings-toggle"
            aria-expanded={advanced}
            aria-controls="run-settings"
            onClick={() => setAdvanced((a) => !a)}
            disabled={busy}
          >
            <span className={"phase-chev" + (advanced ? " open" : "")} aria-hidden="true">
              {Icon.chevron}
            </span>
            <span className="run-settings-summary">{settingsSummary(config)}</span>
            <span className="run-settings-more">{advanced ? "Hide" : "Advanced"}</span>
          </button>

          <div className="composer-submit">
            <span className="composer-hint" aria-hidden="true">
              ⌘↵
            </span>
            {blocker ? (
              <Link className="btn btn-primary" href={blocker.href}>
                {blocker.action}
                {Icon.arrowRight}
              </Link>
            ) : (
              <button
                className="btn btn-primary"
                disabled={!idea.trim() || busy || checking}
                onClick={start}
              >
                {busy && <span className="btn-spinner" aria-hidden="true" />}
                {busy ? "Starting…" : "Start build"}
                {!busy && Icon.arrowRight}
              </button>
            )}
          </div>
        </div>

        {advanced && (
          <div id="run-settings" className="run-settings">
            <RunSettings
              config={config}
              onChange={setConfig}
              options={options}
              disabled={busy}
            />
          </div>
        )}
      </div>

      {blocker && (
        <div className="notice notice-warn" role="status" style={{ marginTop: 14 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">{blocker.title}</span>
            <span className="notice-text">{blocker.text}</span>
            <div className="notice-actions">
              <Link className="btn btn-sm btn-primary" href={blocker.href}>
                {blocker.action}
              </Link>
              <button className="btn btn-sm" onClick={probe} disabled={checking}>
                {checking && <span className="btn-spinner" aria-hidden="true" />}
                {checking ? "Checking…" : "Check again"}
              </button>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div className="notice notice-bad" role="alert" style={{ marginTop: 14 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Couldn&apos;t start the build</span>
            <span className="notice-text">{error}</span>
            {createdId && (
              <div className="notice-actions">
                {/* The project exists even though it never ran — say so, and offer the
                    way in, rather than leaving an orphan nobody knows about. */}
                <Link className="btn btn-sm" href={`/projects/${createdId}`}>
                  Open it anyway
                </Link>
              </div>
            )}
          </div>
        </div>
      )}

      <div className="examples">
        {EXAMPLES.map((ex) => (
          <button key={ex} className="example" onClick={() => setIdea(ex)}>
            {ex}
          </button>
        ))}
      </div>

      <section className="crew">
        <div className="sec-head">
          <h2 className="label">The crew · runs in this order</h2>
          <span className="rule" />
          <Link className="link" href="/crew" style={{ fontSize: "var(--t-xs)" }}>
            Visit the floor
          </Link>
        </div>
        <div className="roster">
          {AGENTS.map((a) => (
            <Link
              key={a.key}
              href="/crew"
              className="agent-card"
              style={{ ["--agent" as string]: a.accent }}
              title={`Meet ${a.codename} on the crew floor`}
            >
              <AgentSprite agent={a} size={48} state="done" />
              <div className="agent-card-body">
                <span className="agent-num">{a.n}</span>
                <h3 className="agent-codename">{a.codename}</h3>
                <span className="agent-role">{a.role}</span>
                <p className="agent-tagline">{a.tagline}</p>
                <span className="agent-trait">{a.trait}</span>
              </div>
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}
