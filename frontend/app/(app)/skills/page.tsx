"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type Skill,
  type SkillDraft,
  type SkillLibrary,
  type SkillPreview,
} from "@/lib/api";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { AGENT_BY_KEY } from "@/components/agents/personas";

/**
 * The skill library: what the crew knows before a build starts.
 *
 * An Operate surface, and the job here is unusually specific. Selection is a
 * keyword score decided before the model is called, which means a miss is
 * *silent* — a skill that does not match never arrives, and nothing in the
 * finished build says so. So this page leads with the dry run rather than with
 * the list: "would this idea actually reach these procedures?" is the question
 * someone opens this page holding, and it is the only one they cannot answer by
 * reading the files.
 *
 * Rows rather than cards, matching Settings: the question a library asks is
 * "what is each of these, and is it on?", which is a scan down one column.
 */

const BLANK: SkillDraft = {
  name: "",
  title: "",
  description: "",
  agents: [],
  keywords: [],
  body: "",
};

export default function SkillsPage() {
  useChrome({ sub: "Skills" }, []);

  const [lib, setLib] = useState<SkillLibrary | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  // Which skill the editor is open on: a name for an edit, "" for a new one,
  // null for closed. Held here rather than in the row so opening a second one
  // closes the first — two open forms is two drafts and one Save button.
  const [editing, setEditing] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setLib(await api.listSkills());
      setError("");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const skills = lib?.skills ?? [];
  const bundled = skills.filter((s) => s.source === "bundled");
  const mine = skills.filter((s) => s.source === "user");

  return (
    <div className="skills-wrap">
      <h1 style={{ fontSize: "var(--t-2xl)" }}>Skills</h1>
      <p className="prose-lede" style={{ marginTop: 10 }}>
        A skill is procedure that holds across projects — how to write an acceptance
        criterion someone else can check, what a paginated endpoint sends back. The
        knowledge base carries facts about <em>your</em> project; these carry craft, and
        they are chosen by keyword and written into the agent&apos;s prompt, so the same
        ones reach a build on a local 7B model and a build on Claude.
      </p>

      {lib && !lib.enabled && (
        <div className="notice notice-warn" style={{ marginTop: 20 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Skills are switched off for this backend</span>
            <span className="notice-text">
              <span className="mono">SKILLS_ENABLED</span> is false, so every build runs
              with no procedures at all. Everything below is still here; nothing below is
              reaching an agent.
            </span>
          </div>
        </div>
      )}

      {error && (
        <div className="notice notice-bad" role="alert" style={{ marginTop: 20 }}>
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Couldn&apos;t read the library</span>
            <span className="notice-text">{error}</span>
            <div className="notice-actions">
              <button className="btn btn-sm" onClick={refresh}>
                Try again
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="skills-stack">
        <DryRun />

        <section className="card">
          <div className="sec-head">
            <h2 className="label">The library</h2>
            <span className="rule" />
            {lib && (
              <span className="dim" style={{ fontSize: "var(--t-xs)" }}>
                up to {lib.max_per_phase} per phase
              </span>
            )}
            <button
              className="btn btn-sm"
              onClick={() => setEditing((e) => (e === "" ? null : ""))}
              aria-expanded={editing === ""}
            >
              {Icon.plus} Add a skill
            </button>
          </div>

          {editing === "" && lib && (
            <Editor
              draft={BLANK}
              phases={lib.phases}
              maxChars={lib.max_chars}
              onDone={() => {
                setEditing(null);
                refresh();
              }}
              onCancel={() => setEditing(null)}
            />
          )}

          {loading && !lib ? (
            <SkeletonLines lines={4} />
          ) : (
            <>
              {mine.length > 0 && (
                <SkillGroup
                  heading="Yours"
                  note={lib ? `saved in ${lib.user_dir}` : ""}
                  skills={mine}
                  lib={lib}
                  editing={editing}
                  setEditing={setEditing}
                  onChanged={refresh}
                />
              )}
              <SkillGroup
                heading={mine.length > 0 ? "Shipped with the platform" : undefined}
                skills={bundled}
                lib={lib}
                editing={editing}
                setEditing={setEditing}
                onChanged={refresh}
              />
              {skills.length === 0 && !loading && (
                <p className="skills-empty">
                  The library is empty, so every phase runs on the idea and the phases
                  before it alone — exactly as this pipeline did before skills existed.
                  Add one above, or drop a <span className="mono">SKILL.md</span> into{" "}
                  <span className="mono">{lib?.user_dir ?? "data/skills"}</span>.
                </p>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}

// ── the dry run ──────────────────────────────────────────────────────────────
/**
 * "Which skills would this idea get, on each phase?"
 *
 * This is the page's reason to exist rather than a convenience. Nothing in a
 * finished build says a skill was *not* chosen, so without asking the question
 * before the run, a keyword that never matches is indistinguishable from a
 * procedure the agent read and ignored.
 */
function DryRun() {
  const [idea, setIdea] = useState("");
  const [result, setResult] = useState<SkillPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function run() {
    const trimmed = idea.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setError("");
    try {
      setResult(await api.previewSkills({ idea: trimmed }));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const empty = result?.phases.filter((p) => p.skills.length === 0) ?? [];

  return (
    <section className="card">
      <div className="sec-head">
        <h2 className="label">Try an idea</h2>
        <span className="rule" />
      </div>
      <p className="field-hint" style={{ marginTop: -6, marginBottom: 12 }}>
        Skills are matched on keywords before the model is called, so a skill that
        misses simply never arrives and the build never mentions it. This is where you
        find that out — before you spend a run on it.
      </p>

      <div className="dryrun-ask">
        <label htmlFor="dryrun-idea" className="sr-only">
          A product idea to test the library against
        </label>
        <input
          id="dryrun-idea"
          className="input"
          placeholder="e.g. a booking site for a dental practice, with online payment"
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") run();
          }}
        />
        <button className="btn btn-primary" onClick={run} disabled={!idea.trim() || busy}>
          {busy && <span className="btn-spinner" aria-hidden="true" />}
          {busy ? "Checking…" : "Check"}
        </button>
      </div>

      {error && (
        <p className="skills-error" role="alert">
          {error}
        </p>
      )}

      {result && (
        <>
          <ol className="dryrun-rows">
            {result.phases.map((p) => {
              const agent = AGENT_BY_KEY[p.phase];
              return (
                <li
                  key={p.phase}
                  className="dryrun-row"
                  style={{ ["--agent" as string]: agent?.accent }}
                >
                  <span className="dryrun-who">
                    <b className="agent-line-name">{agent?.codename ?? p.phase}</b>
                    <span className="dryrun-what">{p.label}</span>
                  </span>
                  {p.skills.length === 0 ? (
                    <span className="dryrun-none">nothing matched</span>
                  ) : (
                    <span className="dryrun-picks">
                      {p.skills.map((s) => (
                        <span
                          key={s.name}
                          className={"pick" + (s.pinned ? " pick-pinned" : "")}
                          title={s.reason}
                        >
                          {s.title}
                        </span>
                      ))}
                    </span>
                  )}
                </li>
              );
            })}
          </ol>
          {empty.length > 0 && (
            <p className="field-hint" style={{ marginTop: 12 }}>
              {empty.length === 1
                ? `${AGENT_BY_KEY[empty[0].phase]?.codename ?? empty[0].label} would get no
                   skills for this idea.`
                : `${empty.length} phases would get no skills for this idea.`}{" "}
              That is a keyword miss, not a verdict — add a keyword below, or pin the
              skill on the build itself from the composer.
            </p>
          )}
          <p className="field-hint" style={{ marginTop: 8 }}>
            Scored on the idea alone. During a real run each phase also sees what the
            phases before it wrote, so the later ones usually match more than this.
          </p>
        </>
      )}
    </section>
  );
}

// ── the library ──────────────────────────────────────────────────────────────
function SkillGroup({
  heading,
  note,
  skills,
  lib,
  editing,
  setEditing,
  onChanged,
}: {
  heading?: string;
  note?: string;
  skills: Skill[];
  lib: SkillLibrary | null;
  editing: string | null;
  setEditing: (name: string | null) => void;
  onChanged: () => void;
}) {
  if (skills.length === 0) return null;
  return (
    <>
      {heading && (
        <p className="skills-group">
          {heading}
          {note && <span className="mono">{note}</span>}
        </p>
      )}
      <ul className="skill-rows">
        {skills.map((skill) => (
          <SkillRow
            key={`${skill.source}-${skill.name}`}
            skill={skill}
            lib={lib}
            editing={editing === skill.name}
            onEdit={() => setEditing(editing === skill.name ? null : skill.name)}
            onChanged={onChanged}
          />
        ))}
      </ul>
    </>
  );
}

function SkillRow({
  skill,
  lib,
  editing,
  onEdit,
  onChanged,
}: {
  skill: Skill;
  lib: SkillLibrary | null;
  editing: boolean;
  onEdit: () => void;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState(false);

  const serves = useMemo(
    () =>
      skill.agents.length === 0
        ? "Every agent"
        : skill.agents
            .map((a) => AGENT_BY_KEY[a]?.codename ?? a)
            .join(" · "),
    [skill.agents],
  );

  async function toggle() {
    setBusy(true);
    setError("");
    try {
      await api.setSkillEnabled(skill.name, !skill.enabled);
      onChanged();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setError("");
    try {
      await api.deleteSkill(skill.name);
      onChanged();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  }

  return (
    <li className={"skill-row" + (skill.enabled && skill.usable ? "" : " off")}>
      <div className="skill-main">
        <button
          className="switch"
          role="switch"
          aria-checked={skill.enabled}
          aria-label={`${skill.enabled ? "Switch off" : "Switch on"} ${skill.title}`}
          disabled={busy || !skill.usable}
          onClick={toggle}
          title={
            skill.usable
              ? "Off means no build gets it unless it names it"
              : "This skill can never be injected — see the reason below"
          }
        >
          <span className="switch-track" aria-hidden="true" />
        </button>

        <button
          className="skill-id"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          <span className="skill-name">
            {skill.title}
            {!skill.usable && <span className="badge badge-bad">Never injected</span>}
            {skill.overridden && (
              <span className="badge" title="Your edit is being used instead of the original">
                Edited
              </span>
            )}
          </span>
          <span className="skill-what">{skill.description}</span>
          <span className="skill-meta">
            <span className="skill-serves">{serves}</span>
            <span className="mono">{skill.chars.toLocaleString()} chars</span>
          </span>
        </button>

        <div className="skill-acts">
          <button className="btn btn-sm" onClick={onEdit} aria-expanded={editing}>
            {Icon.pen} Edit
          </button>
          {skill.source === "user" &&
            (confirming ? (
              <>
                <button className="btn btn-sm" onClick={() => setConfirming(false)}>
                  Keep
                </button>
                <button className="btn btn-sm btn-danger" onClick={remove} disabled={busy}>
                  {busy && <span className="btn-spinner" aria-hidden="true" />}
                  {skill.overridden ? "Restore" : "Delete"}
                </button>
              </>
            ) : (
              <button
                className="btn btn-sm btn-ghost"
                aria-label={
                  skill.overridden
                    ? `Restore the original ${skill.title}`
                    : `Delete ${skill.title}`
                }
                title={
                  skill.overridden
                    ? "Drop your edit and use the original again"
                    : "Delete this skill"
                }
                onClick={() => setConfirming(true)}
              >
                {skill.overridden ? Icon.undo : Icon.trash}
              </button>
            ))}
        </div>
      </div>

      {skill.keywords.length > 0 && (
        <p className="skill-keys">
          {skill.keywords.map((k) => (
            <span key={k} className="key">
              {k}
            </span>
          ))}
        </p>
      )}

      {!skill.usable && (
        <p className="skill-problem">
          {skill.problems.join(" ")}
        </p>
      )}

      {error && (
        <p className="skills-error" role="alert">
          {error}
        </p>
      )}

      {open && <pre className="skill-body">{skill.body}</pre>}

      {editing && lib && (
        <Editor
          draft={{
            name: skill.name,
            title: skill.title,
            description: skill.description,
            agents: skill.agents,
            keywords: skill.keywords,
            body: skill.body,
          }}
          locked={skill.name}
          bundled={skill.source === "bundled"}
          phases={lib.phases}
          maxChars={lib.max_chars}
          onDone={() => {
            onEdit();
            onChanged();
          }}
          onCancel={onEdit}
        />
      )}
    </li>
  );
}

// ── the editor ───────────────────────────────────────────────────────────────
function Editor({
  draft,
  locked,
  bundled,
  phases,
  maxChars,
  onDone,
  onCancel,
}: {
  draft: SkillDraft;
  /** The name being edited. Absent when this is a new skill. */
  locked?: string;
  /** True when saving will shadow a skill that ships with the platform. */
  bundled?: boolean;
  phases: { key: string; label: string }[];
  maxChars: number;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<SkillDraft>(draft);
  const [keywords, setKeywords] = useState(draft.keywords.join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const over = form.body.length > maxChars;
  const slug = (locked ?? form.name ?? "").trim();

  async function save() {
    if (busy) return;
    setBusy(true);
    setError("");
    const body: SkillDraft = {
      ...form,
      name: slug,
      keywords: keywords
        .split(",")
        .map((k) => k.trim())
        .filter(Boolean),
    };
    try {
      if (locked) await api.updateSkill(locked, body);
      else await api.createSkill(body);
      onDone();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="skill-editor">
      {bundled && (
        <p className="field-hint">
          This one ships with the platform. Saving keeps the original where it is and
          uses your version instead — and Restore hands the original back.
        </p>
      )}

      <div className="skill-form">
        <div className="field skill-f-name">
          <label htmlFor="sk-name">Name</label>
          <input
            id="sk-name"
            className="input input-mono"
            placeholder="api-contract-design"
            value={slug}
            disabled={Boolean(locked) || busy}
            onChange={(e) =>
              setForm({ ...form, name: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") })
            }
          />
          <p className="field-hint">Lowercase, hyphens. Also the folder it is saved in.</p>
        </div>

        <div className="field skill-f-title">
          <label htmlFor="sk-title">Title</label>
          <input
            id="sk-title"
            className="input"
            placeholder="API contract design"
            value={form.title}
            disabled={busy}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
          />
        </div>

        <div className="field skill-f-wide">
          <label htmlFor="sk-desc">When it applies</label>
          <input
            id="sk-desc"
            className="input"
            placeholder="Use when defining HTTP endpoints — paths, shapes, status codes…"
            value={form.description}
            disabled={busy}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
          <p className="field-hint">
            An agent given a procedure with no trigger applies it to everything.
          </p>
        </div>

        <div className="field skill-f-wide">
          <span className="label" id="sk-agents">
            Who may receive it
          </span>
          <div className="agent-picks" role="group" aria-labelledby="sk-agents">
            {phases.map((p) => {
              const on = form.agents.includes(p.key);
              const agent = AGENT_BY_KEY[p.key];
              return (
                <button
                  key={p.key}
                  type="button"
                  className="agent-pick"
                  aria-pressed={on}
                  disabled={busy}
                  style={{ ["--agent" as string]: agent?.accent }}
                  onClick={() =>
                    setForm({
                      ...form,
                      agents: on
                        ? form.agents.filter((a) => a !== p.key)
                        : [...form.agents, p.key],
                    })
                  }
                >
                  <span className="dot" aria-hidden="true" />
                  {agent?.codename ?? p.label}
                </button>
              );
            })}
          </div>
          <p className="field-hint">
            {form.agents.length === 0
              ? "None chosen — every agent may receive it."
              : `${form.agents.length} of ${phases.length} phases.`}
          </p>
        </div>

        <div className="field skill-f-wide">
          <label htmlFor="sk-keys">Keywords</label>
          <input
            id="sk-keys"
            className="input input-mono"
            placeholder="api, rest, endpoint, pagination"
            value={keywords}
            disabled={busy}
            onChange={(e) => setKeywords(e.target.value)}
          />
          <p className="field-hint">
            Matched whole-word against the idea, the phase and what earlier phases
            wrote. No keywords means it is relevant to every build its agents run in.
          </p>
        </div>

        <div className="field skill-f-wide">
          <label htmlFor="sk-body">The procedure</label>
          <textarea
            id="sk-body"
            className="textarea"
            rows={12}
            placeholder={"Name resources as plural nouns and act on them with methods…"}
            value={form.body}
            disabled={busy}
            onChange={(e) => setForm({ ...form, body: e.target.value })}
          />
          <p className={"field-hint" + (over ? " over" : "")}>
            <span className="mono">
              {form.body.length.toLocaleString()} / {maxChars.toLocaleString()}
            </span>{" "}
            — every phase that gets this pays for all of it, so what you spend here the
            knowledge base and the earlier phases do not get. Say what to do, never how
            to lay the answer out: each agent already answers in a fixed shape, and a
            procedure that argues with it costs the build a repair round.
          </p>
        </div>
      </div>

      {error && (
        <p className="skills-error" role="alert">
          {error}
        </p>
      )}

      <div className="skill-editor-acts">
        <button className="btn btn-primary" onClick={save} disabled={busy || !slug}>
          {busy && <span className="btn-spinner" aria-hidden="true" />}
          {busy ? "Saving…" : locked ? "Save changes" : "Add skill"}
        </button>
        <button className="btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  );
}
