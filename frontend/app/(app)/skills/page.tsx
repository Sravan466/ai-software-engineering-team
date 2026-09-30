"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  type Skill,
  type SkillDraft,
  type SkillLibrary,
  type SkillPreview,
} from "@/lib/api";
import { useChrome } from "@/components/shell/ShellChrome";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import PreviewRows from "@/components/skills/PreviewRows";
import { alwaysMatching, byTitle, forgetSkillTitles } from "@/components/skills/skills";

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
      // A build page opened after this reads titles fresh, not the ones from
      // before an edit.
      forgetSkillTitles();
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
  const bundled = skills.filter((s) => s.source === "bundled").sort(byTitle);
  const mine = skills.filter((s) => s.source === "user").sort(byTitle);
  const canEdit = lib?.can_edit ?? false;

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
            {canEdit && (
              <button
                className="btn btn-sm"
                onClick={() => setEditing((e) => (e === "" ? null : ""))}
                aria-expanded={editing === ""}
              >
                {Icon.plus} Add a skill
              </button>
            )}
          </div>

          {lib && !canEdit && (
            <p className="field-hint skills-readonly">
              The library is shared by every account on this install, so only its owner
              can add, edit or switch skills. You can read all of them, and pin or skip
              any of them on your own builds from the composer.
            </p>
          )}

          {editing === "" && lib && canEdit && (
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
                  canEdit={canEdit}
                  editing={editing}
                  setEditing={setEditing}
                  onChanged={refresh}
                />
              )}
              <SkillGroup
                heading={mine.length > 0 ? "Shipped with the platform" : undefined}
                skills={bundled}
                lib={lib}
                canEdit={canEdit}
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
  const crowded = result?.phases.filter((p) => (p.over_cap ?? []).length > 0) ?? [];

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
          <PreviewRows preview={result} showLabels />
          {empty.length > 0 && (
            <p className="field-hint" style={{ marginTop: 12 }}>
              {empty.length === 1
                ? `${AGENT_BY_KEY[empty[0].phase]?.codename ?? empty[0].label} would get no skills for this idea.`
                : `${empty.length} phases would get no skills for this idea.`}{" "}
              That is a keyword miss, not a verdict — add a keyword below, or pin the
              skill on the build itself from the composer.
            </p>
          )}
          {crowded.length > 0 && (
            <p className="field-hint" style={{ marginTop: 12 }}>
              {crowded.length === 1
                ? `${AGENT_BY_KEY[crowded[0].phase]?.codename ?? crowded[0].label} matched more skills than it can take.`
                : `${crowded.length} phases matched more skills than they can take.`}{" "}
              Each phase gets at most {result.max_per_phase}, strongest match first, so the
              ones listed as left out never reach the model. Pin one on the build to put it
              first, or tighten a stronger skill&apos;s keywords.
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
  canEdit,
  editing,
  setEditing,
  onChanged,
}: {
  heading?: string;
  note?: string;
  skills: Skill[];
  lib: SkillLibrary | null;
  canEdit: boolean;
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
            canEdit={canEdit}
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
  canEdit,
  editing,
  onEdit,
  onChanged,
}: {
  skill: Skill;
  lib: SkillLibrary | null;
  canEdit: boolean;
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
  const everywhere = useMemo(
    () => (lib ? alwaysMatching(skill.keywords, skill.agents, lib.phases) : []),
    [lib, skill.keywords, skill.agents],
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
          disabled={busy || !skill.usable || !canEdit}
          onClick={toggle}
          title={
            !canEdit
              ? "Only the install's owner can switch skills"
              : skill.usable
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
          {canEdit && (
            <button className="btn btn-sm" onClick={onEdit} aria-expanded={editing}>
              {Icon.pen} Edit
            </button>
          )}
          {canEdit &&
            skill.source === "user" &&
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

      {everywhere.length > 0 && (
        <p className="skill-warn">
          {Icon.alert}
          <span>
            <EverywhereText hits={everywhere} /> It arrives whether or not the idea has
            anything to do with it.
          </span>
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

      {editing && lib && canEdit && (
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
/** The server's `NAME_RE`: a slug that starts and ends with a letter or digit. */
const NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$/;

/** Typing a name: anything that is not a slug character becomes one hyphen. A
 *  trailing hyphen is left while typing — it is how "api-" becomes "api-design" —
 *  and trimmed when the field is left or saved. */
function slugify(raw: string): string {
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+/, "");
}

type Field = "name" | "title" | "description" | "body";

/**
 * "`frontend` is in Prism's own phase name" — the sentence both the row and the
 * editor print for keywords that match every build.
 */
function EverywhereText({ hits }: { hits: { keyword: string; who: string[] }[] }) {
  const words = hits.map((h) => h.keyword);
  const who = Array.from(new Set(hits.flatMap((h) => h.who)));
  return (
    <>
      {words.map((w, n) => (
        <span key={w}>
          {n > 0 && (n === words.length - 1 ? " and " : ", ")}
          <span className="mono">{w}</span>
        </span>
      ))}{" "}
      {words.length === 1 ? "is" : "are"} in {who.join(" and ")}&apos;s own phase name,
      so {words.length === 1 ? "it matches" : "they match"} every build{" "}
      {who.length === 1 ? "that phase runs" : "those phases run"}.
    </>
  );
}

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
  phases: SkillLibrary["phases"];
  maxChars: number;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState<SkillDraft>(draft);
  const [keywords, setKeywords] = useState(draft.keywords.join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // What the server refused about the name itself — taken, usually. Shown on the
  // field it is about, and dropped the moment the name changes.
  const [nameTaken, setNameTaken] = useState("");
  // Problems show once Save has been tried, then follow every keystroke. Before
  // that, a blank form shouting four errors at someone who has not typed is noise.
  const [tried, setTried] = useState(false);
  const refs = {
    name: useRef<HTMLInputElement>(null),
    title: useRef<HTMLInputElement>(null),
    description: useRef<HTMLInputElement>(null),
    body: useRef<HTMLTextAreaElement>(null),
  };

  // What this skill will actually cost a prompt. The title and the description are
  // injected beside the procedure, so counting the textarea alone under-reports the
  // bill by the length of two fields the author can see but not feel.
  // Spread rather than `.length`: JavaScript counts UTF-16 code units and the
  // server counts code points, so an emoji in a title made the two disagree — the
  // counter on screen is meant to be the number the ceiling is checked against.
  const cost = [
    ...`## ${form.title.trim()}${form.description.trim() ? `\n${form.description.trim()}` : ""}\n${form.body.trim()}`.trim(),
  ].length;
  const over = cost > maxChars;
  const slug = (locked ?? form.name ?? "").trim().replace(/-+$/, "");
  const keywordList = keywords
    .split(",")
    .map((k) => k.trim())
    .filter(Boolean);
  const everywhere = alwaysMatching(keywordList, form.agents, phases);

  // The same rules the server applies on save, said next to the field they are
  // about. The server still checks — it is the authority — and anything it refuses
  // that is not listed here (a format instruction in the body) shows under the form.
  const problems: Partial<Record<Field, string>> = {};
  if (!locked && !NAME_RE.test(slug))
    problems.name = slug
      ? "Use 2–64 lowercase letters, digits and hyphens, starting and ending with a letter or digit."
      : "Give it a name — it is the folder it is saved in, and what a build pins it by.";
  else if (nameTaken) problems.name = nameTaken;
  if (!form.title.trim()) problems.title = "Give it a title — it is what every list shows.";
  if (!form.description.trim())
    problems.description = "Say when it applies — it is the one line the preview can show.";
  if (!form.body.trim()) problems.body = "Write the procedure the agent will follow.";
  else if (over)
    problems.body = `${cost.toLocaleString()} characters is over the ${maxChars.toLocaleString()} ceiling. Cut the procedure, or the title and description beside it.`;
  const shown = (f: Field) => (tried || (f === "name" && nameTaken) ? problems[f] : undefined);

  async function save() {
    if (busy) return;
    setTried(true);
    const first = (["name", "title", "description", "body"] as Field[]).find((f) => problems[f]);
    if (first) {
      refs[first].current?.focus();
      return;
    }
    setBusy(true);
    setError("");
    const body: SkillDraft = { ...form, name: slug, keywords: keywordList };
    try {
      if (locked) await api.updateSkill(locked, body);
      else await api.createSkill(body);
      onDone();
    } catch (e: any) {
      if (e instanceof ApiError && e.status === 409 && !locked) {
        setNameTaken(e.message);
        refs.name.current?.focus();
      } else {
        setError(e.message);
      }
    } finally {
      setBusy(false);
    }
  }

  /** aria wiring for one field: its hint, and its problem when there is one. */
  const described = (f: Field, hint?: boolean) => ({
    "aria-invalid": shown(f) ? true : undefined,
    "aria-describedby":
      [hint ? `sk-${f}-hint` : "", shown(f) ? `sk-${f}-err` : ""].filter(Boolean).join(" ") ||
      undefined,
  });
  const problem = (f: Field) =>
    shown(f) ? (
      <p className="field-error" id={`sk-${f}-err`}>
        {Icon.alert}
        <span>{shown(f)}</span>
      </p>
    ) : null;

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
            ref={refs.name}
            className="input input-mono"
            placeholder="api-contract-design"
            value={locked ?? form.name ?? ""}
            disabled={Boolean(locked) || busy}
            onChange={(e) => {
              setNameTaken("");
              setForm({ ...form, name: slugify(e.target.value) });
            }}
            onBlur={() => !locked && setForm((f) => ({ ...f, name: (f.name ?? "").replace(/-+$/, "") }))}
            {...described("name", true)}
          />
          {problem("name")}
          <p className="field-hint" id="sk-name-hint">
            Lowercase, hyphens. Also the folder it is saved in.
          </p>
        </div>

        <div className="field skill-f-title">
          <label htmlFor="sk-title">Title</label>
          <input
            id="sk-title"
            ref={refs.title}
            className="input"
            placeholder="API contract design"
            value={form.title}
            disabled={busy}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
            {...described("title")}
          />
          {problem("title")}
        </div>

        <div className="field skill-f-wide">
          <label htmlFor="sk-desc">When it applies</label>
          <input
            id="sk-desc"
            ref={refs.description}
            className="input"
            placeholder="Use when defining HTTP endpoints — paths, shapes, status codes…"
            value={form.description}
            disabled={busy}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            {...described("description", true)}
          />
          {problem("description")}
          <p className="field-hint" id="sk-description-hint">
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
            aria-describedby={"sk-keys-hint" + (everywhere.length ? " sk-keys-warn" : "")}
          />
          {everywhere.length > 0 && (
            <p className="field-warn" id="sk-keys-warn" aria-live="polite">
              {Icon.alert}
              <span>
                <EverywhereText hits={everywhere} /> {everywhere.length === 1 ? "It says" : "They say"}{" "}
                nothing about the idea — drop {everywhere.length === 1 ? "it" : "them"}, or
                leave the skill with no keywords if it really belongs on every build.
              </span>
            </p>
          )}
          <p className="field-hint" id="sk-keys-hint">
            Matched whole-word against the idea, the phase and what earlier phases
            wrote. No keywords means it is relevant to every build its agents run in.
          </p>
        </div>

        <div className="field skill-f-wide">
          <label htmlFor="sk-body">The procedure</label>
          <textarea
            id="sk-body"
            ref={refs.body}
            className="textarea"
            rows={12}
            placeholder={"Name resources as plural nouns and act on them with methods…"}
            value={form.body}
            disabled={busy}
            onChange={(e) => setForm({ ...form, body: e.target.value })}
            {...described("body", true)}
          />
          {problem("body")}
          <p className={"field-hint" + (over ? " over" : "")} id="sk-body-hint">
            <span className="mono">
              {cost.toLocaleString()} / {maxChars.toLocaleString()}
            </span>{" "}
            — the title and the description count too, because they are injected beside
            the procedure. Every phase that gets this pays for all of it, so what you
            spend here the knowledge base and the earlier phases do not get. Say what to
            do, never how to lay the answer out: each agent already answers in a fixed
            shape, and a procedure that argues with it costs the build a repair round.
          </p>
        </div>
      </div>

      {error && (
        <p className="skills-error" role="alert">
          {error}
        </p>
      )}

      <div className="skill-editor-acts">
        <button className="btn btn-primary" onClick={save} disabled={busy}>
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
