"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import {
  api,
  type ChangePlan,
  type ChangeRequest,
  type Diff,
  type DiffFile,
  type Project,
} from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import { Icon } from "@/components/shell/icons";
import { ago, useNow } from "@/components/setup/parts";
import { listOf } from "@/lib/text";

/**
 * Keep talking after a build (#79).
 *
 * A finished build used to end in a notice with three doors out — Deploy, GitHub,
 * Deliver — and nothing to say to the crew. The notice is still the end of the run,
 * but it now carries the conversation: what to change next, and what was changed
 * before. While a change is being made the same slot follows it, and its review is
 * the Ship review, opened on what changed.
 */

type Act = (fn: () => Promise<unknown>) => Promise<boolean>;

const EXAMPLES = [
  "Add login with email + password",
  "Make the list a sortable table",
  "Validate email on the signup form",
];

const codename = (phase: string) => AGENT_BY_KEY[phase]?.codename ?? phase;


/** "ATLAS updates the design, then FORGE changes about 2 files. SIEVE adds tests,
 *  WARDEN rescans, LEDGER re-estimates." — the plan, said with the crew's names. */
export function planSentence(plan: ChangePlan | null): string {
  if (!plan) return "";
  const editors = plan.phases.map(codename);
  const files = plan.files_likely.length;
  const doing =
    editors.length === 0
      ? "The crew makes the change"
      : files > 0
        ? `${listOf(editors)} ${editors.length === 1 ? "changes" : "change"} about ${files} file${files === 1 ? "" : "s"}`
        : `${listOf(editors)} ${editors.length === 1 ? "makes" : "make"} the change`;
  const first = plan.needs_design ? `${codename("system_design")} updates the design, then ${doing.charAt(0).toLowerCase()}${doing.slice(1)}` : doing;
  return `${first}. ${codename("qa_engineer")} adds tests, ${codename("security_engineer")} rescans, ${codename("cost_estimation")} re-estimates.`;
}

const STATE_WORD: Record<string, string> = {
  done: "Kept",
  failed: "Didn't run",
  discarded: "Discarded",
  planning: "Planning",
  running: "In progress",
  awaiting_approval: "Waiting for review",
  needs_help: "Needs a hand",
  stopped: "Stopped",
};

// ── the finished build, and what to change next ──────────────────────────────
export function Finished({
  project,
  id,
  busy,
  act,
  doneCount,
  total,
  onDeliver,
}: {
  project: Project;
  id: string;
  busy: boolean;
  act: Act;
  doneCount: number;
  total: number;
  onDeliver: (intent?: "deploy" | "github") => void;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [history, setHistory] = useState<ChangeRequest[] | null>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const version = project.current_version;
  const now = useNow(30000);

  useEffect(() => {
    let live = true;
    api
      .listChanges(id)
      .then((r) => live && setHistory(r.changes))
      .catch(() => live && setHistory([]));
    return () => {
      live = false;
    };
  }, [id, version?.number, project.updated_at]);

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const ask = text.trim();
    if (!ask || sending || busy) return;
    setSending(true);
    const ok = await act(() => api.startChange(id, ask));
    setSending(false);
    // Kept on a refusal, so nothing typed is lost to a 409 from another tab.
    if (ok) setText("");
  }

  function pick(example: string) {
    setText(example);
    field.current?.focus();
  }

  const recent = (history ?? []).slice(0, 5);

  return (
    <section className="finish" aria-labelledby="finish-title">
      <div className="finish-done">
        <span className="finish-mark" aria-hidden="true">
          {Icon.check}
        </span>
        <div className="finish-body">
          <h2 id="finish-title" className="finish-title">
            {doneCount === total ? `All ${total === 8 ? "eight" : total} phases approved` : `Finished with ${doneCount} of ${total} phases approved`}
            {version && (
              <span className="finish-version" title={`Version ${version.number}: ${version.label}`}>
                <b className="mono">v{version.number}</b>
                <span>{version.label}</span>
              </span>
            )}
          </h2>
          <p className="finish-text">Deploy it, push it to GitHub, or download a .zip from Deliver.</p>
          <div className="notice-actions">
            <button className="btn btn-sm btn-primary" onClick={() => onDeliver("deploy")}>
              Deploy it
            </button>
            <button className="btn btn-sm" onClick={() => onDeliver("github")}>
              {Icon.github} Connect to GitHub
            </button>
            <button className="btn btn-sm btn-ghost" onClick={() => onDeliver()}>
              Open Deliver {Icon.arrowRight}
            </button>
          </div>
        </div>
      </div>

      <form className="finish-ask" onSubmit={send}>
        <label htmlFor="change-text" className="finish-label">
          What should change?
        </label>
        <textarea
          id="change-text"
          ref={field}
          rows={2}
          maxLength={2000}
          placeholder="e.g. add a dark mode toggle to the header"
          value={text}
          disabled={sending}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
          }}
          aria-describedby="change-hint"
        />
        <div className="finish-foot">
          <div className="examples finish-examples" role="group" aria-label="Examples">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                className="example"
                onClick={() => pick(example)}
                disabled={sending}
              >
                {example}
              </button>
            ))}
          </div>
          <div className="composer-submit">
            <span className="composer-hint" aria-hidden="true">
              ⌘↵
            </span>
            <button className="btn btn-primary" type="submit" disabled={!text.trim() || sending || busy}>
              {sending && <span className="btn-spinner" aria-hidden="true" />}
              {sending ? "Sending…" : "Make this change"}
              {!sending && Icon.arrowRight}
            </button>
          </div>
        </div>
        <p id="change-hint" className="field-hint finish-hint">
          The crew edits the files it wrote, then builds, tests and scans the result.
          {version ? ` v${version.number} keeps shipping until you keep the change.` : ""}
        </p>
      </form>

      {recent.length > 0 && (
        <div className="finish-ledger">
          <h3 className="label">Earlier changes</h3>
          <ol className="ledger">
            {recent.map((c) => (
              <li key={c.id} className="ledger-row" data-state={c.status}>
                <span className="ledger-num mono">#{c.number}</span>
                <span className="ledger-text" title={c.note ?? c.text}>
                  {c.text}
                </span>
                <span className="ledger-state">
                  {c.status === "done" && c.version ? (
                    <>
                      {Icon.check} <b className="mono">v{c.version}</b>
                    </>
                  ) : (
                    STATE_WORD[c.status] ?? c.status
                  )}
                </span>
                <span className="ledger-time">{ago(c.finished_at || c.created_at, now)}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}

// ── a change being made ──────────────────────────────────────────────────────
/**
 * The change in flight, in the slot the finished notice held: what was asked, who
 * is on it, and — once it has stopped anywhere but its review — the way back to the
 * version it was made on. Its review has its own surface (the Ship review); this
 * strip stays out of the way there.
 */
export function ChangeStrip({ project, id, busy, act }: { project: Project; id: string; busy: boolean; act: Act }) {
  const change = project.change;
  if (!change) return null;
  const state = change.status;
  const version = project.current_version;
  const discardable = state !== "planning" && state !== "running";
  return (
    <section className="change-strip" data-state={state} aria-labelledby="change-strip-title">
      <div className="change-strip-head">
        <span className="change-strip-num mono">#{change.number}</span>
        <h2 id="change-strip-title" className="change-strip-text">
          {change.text}
        </h2>
        <span className={`badge ${state === "needs_help" ? "badge-warn" : state === "stopped" ? "" : "badge-run"}`}>
          {(state === "planning" || state === "running") && <span className="dot dot-run dot-pulse" aria-hidden="true" />}
          {STATE_WORD[state] ?? state}
        </span>
      </div>
      <p className="change-strip-plan" aria-live="polite" key={change.plan ? "plan" : "planning"}>
        {change.plan ? planSentence(change.plan) : "Reading the app to work out who changes what…"}
      </p>
      <p className="field-hint change-strip-foot">
        {version ? `v${version.number} — ${version.label} — keeps shipping until you keep this change.` : "Nothing ships from this change until you keep it."}
        {change.plan?.guessed ? " The planner named nobody, so the request's own words picked who edits." : ""}
      </p>
      {discardable && <DiscardChange project={project} id={id} busy={busy} act={act} />}
    </section>
  );
}

/** Throw the open change away, with one inline confirmation — no modal. */
export function DiscardChange({
  project,
  id,
  busy,
  act,
  compact = false,
}: {
  project: Project;
  id: string;
  busy: boolean;
  act: Act;
  compact?: boolean;
}) {
  const [asking, setAsking] = useState(false);
  const change = project.change;
  if (!change) return null;
  const back = project.current_version ? `v${project.current_version.number}` : "the version before it";
  if (!asking) {
    return (
      <button
        type="button"
        className={`btn ${compact ? "btn-sm " : ""}btn-ghost change-discard`}
        disabled={busy}
        onClick={() => setAsking(true)}
      >
        {Icon.undo} Discard change
      </button>
    );
  }
  return (
    <div className="change-confirm" role="group" aria-label="Confirm discarding the change">
      <span>
        Discard change #{change.number}? The crew&apos;s work on it is dropped and {back} stays.
      </span>
      <button
        type="button"
        className="btn btn-sm btn-danger"
        disabled={busy}
        autoFocus
        onClick={() => act(() => api.discardChange(id, change.id))}
      >
        Discard
      </button>
      <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={() => setAsking(false)}>
        Keep it
      </button>
    </div>
  );
}

// ── what changed ─────────────────────────────────────────────────────────────
const STATUS: Record<DiffFile["status"], { word: string; icon: keyof typeof Icon }> = {
  added: { word: "Added", icon: "plus" },
  changed: { word: "Changed", icon: "pen" },
  deleted: { word: "Removed", icon: "trash" },
};

/**
 * A file-by-file diff: each file a disclosure with its +/− counts, open on the first
 * few. Every line keeps its own +/− mark and every file says Added, Changed or
 * Removed in words, so nothing here is told by colour alone.
 */
export function ChangeDiff({
  load,
  reloadKey,
  empty = "Nothing in the code changed.",
}: {
  load: () => Promise<Diff>;
  reloadKey?: unknown;
  empty?: string;
}) {
  const [diff, setDiff] = useState<Diff | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());

  useEffect(() => {
    let live = true;
    setError("");
    load()
      .then((d) => {
        if (!live) return;
        setDiff(d);
        setOpen(new Set(d.files.slice(0, d.files.length <= 4 ? 4 : 2).map((f) => f.path)));
      })
      .catch((e: any) => live && setError(e.message));
    return () => {
      live = false;
    };
    // `load` is a fresh closure every render; what it fetches is keyed by `reloadKey`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadKey]);

  if (error) {
    return (
      <p className="field-error diff-note" role="alert">
        {Icon.alert}
        <span>Couldn&apos;t load what changed: {error}</span>
      </p>
    );
  }
  if (!diff) {
    return (
      <div className="diff-skeleton" aria-busy="true" aria-label="Loading what changed">
        <span />
        <span />
        <span />
      </div>
    );
  }
  if (diff.files.length === 0) return <p className="diff-note field-hint">{empty}</p>;

  const plus = diff.files.reduce((n, f) => n + f.added, 0);
  const minus = diff.files.reduce((n, f) => n + f.removed, 0);
  const parts = (["changed", "added", "deleted"] as const)
    .filter((k) => diff.counts[k])
    .map((k) => `${diff.counts[k]} ${STATUS[k].word.toLowerCase()}`);

  return (
    <div className="diff">
      <p className="diff-sum">
        <span>
          {diff.files.length} file{diff.files.length === 1 ? "" : "s"} · {parts.join(", ")}
          {diff.base ? ` since v${diff.base}` : ""}
        </span>
        <span className="diff-counts mono">
          <span className="diff-plus">+{plus}</span> <span className="diff-minus">−{minus}</span>
        </span>
      </p>
      <ul className="diff-files">
        {diff.files.map((f) => (
          <DiffEntry
            key={f.path}
            file={f}
            open={open.has(f.path)}
            onToggle={() =>
              setOpen((s) => {
                const next = new Set(s);
                if (next.has(f.path)) next.delete(f.path);
                else next.add(f.path);
                return next;
              })
            }
          />
        ))}
      </ul>
    </div>
  );
}

function DiffEntry({ file, open, onToggle }: { file: DiffFile; open: boolean; onToggle: () => void }) {
  const bodyId = useId();
  const status = STATUS[file.status];
  const who = file.phase && file.phase !== "platform" ? codename(file.phase) : file.phase === "platform" ? "platform" : null;
  return (
    <li className="diff-file" data-status={file.status}>
      <button type="button" className="diff-head" aria-expanded={open} aria-controls={bodyId} onClick={onToggle}>
        <span className={"phase-chev" + (open ? " open" : "")} aria-hidden="true">
          {Icon.chevron}
        </span>
        <span className="diff-status">
          {Icon[status.icon]}
          {status.word}
        </span>
        <span className="diff-path mono">{file.path}</span>
        {who && <span className="diff-who">{who}</span>}
        <span className="diff-counts mono">
          {file.added > 0 && <span className="diff-plus">+{file.added}</span>}
          {file.removed > 0 && <span className="diff-minus">−{file.removed}</span>}
        </span>
      </button>
      {/* Opens its own height (grid rows 0fr → 1fr); closed, it is hidden from focus and
          from assistive tech by `visibility`, which waits for the fold to finish. */}
      <div className="diff-body" id={bodyId} data-open={open}>
        <div className="diff-clip">
          <pre className="diff-lines mono">
            {file.lines.map((line, i) => {
              const kind = line.startsWith("@@") ? "hunk" : line[0] === "+" ? "add" : line[0] === "-" ? "del" : "ctx";
              return (
                <span key={i} className={`dl dl-${kind}`}>
                  {kind === "hunk" ? line : (
                    <>
                      <span className="dl-mark" aria-hidden={kind === "ctx"}>
                        {kind === "add" ? "+" : kind === "del" ? "−" : " "}
                      </span>
                      {line.slice(1) || " "}
                    </>
                  )}
                  {"\n"}
                </span>
              );
            })}
          </pre>
          {file.truncated && (
            <p className="field-hint diff-more">The rest of this file is in Files and in the download.</p>
          )}
        </div>
      </div>
    </li>
  );
}
