"use client";

import { useEffect, useMemo, useState } from "react";
import type { HandoffDep, PhaseResult } from "@/lib/api";
import { AGENT_BY_KEY } from "@/components/agents/personas";
import Markdown from "@/components/ui/Markdown";
import { Icon } from "@/components/shell/icons";
import DetailFields from "./DetailFields";
import { HowWritten } from "./CodeWriting";
import BuildRunLine from "./BuildRunLine";
import TestRunLine from "./TestRunLine";
import ScanLine from "./ScanLine";
import { samePath } from "@/lib/openFile";

import FileBrowser, { type FileFocus } from "./FileBrowser";
import Mermaid from "./Mermaid";
import { useSkillTitles } from "@/components/skills/skills";
import { extractFiles, extractMermaid, fileKeys, fileSummary, toFields } from "./payload";

/**
 * Everything one agent produced, in the four shapes it comes in.
 *
 * The gate used to render `content_md` and nothing else — so "approve `frontend/`"
 * was a decision made from two sentences while seven source files sat unread in the
 * same response. This is the fix: the summary still leads (outcome first), and the
 * artifact itself is one click away in the same panel, never a different screen.
 *
 * Views are built from what the payload actually contains, so a phase with no files
 * shows no Files tab rather than an empty one, and a single-view phase shows no
 * switcher at all.
 */

type ViewKey = "summary" | "files" | "diagram" | "details";

type View = {
  key: ViewKey;
  label: string;
  icon: React.ReactNode;
  count?: number;
};

export default function PhaseArtifact({
  row,
  /** Where the scroll area tops out. The gate gives its artifact more room than
   *  a browsing disclosure does, because that is the moment it matters. */
  maxHeight = 420,
  focus,
  onFocused,
}: {
  row: PhaseResult;
  maxHeight?: number;
  /** A file a finding asked to see (#77): opens Files on it, at its line. */
  focus?: FileFocus;
  /** Told once the ask is taken, so the parent can forget it: a remount mustn't
   *  snap the view back to Files. */
  onFocused?: () => void;
}) {
  const { files, mermaid, fields } = useMemo(() => {
    const output = row.output || {};
    return {
      files: extractFiles(output),
      mermaid: extractMermaid(output),
      fields: toFields(output, fileKeys(output)),
    };
  }, [row.output]);

  const views: View[] = [];
  if (row.content_md) views.push({ key: "summary", label: "Summary", icon: Icon.list });
  if (files.length) {
    views.push({ key: "files", label: "Files", icon: Icon.file, count: files.length });
  }
  if (mermaid) views.push({ key: "diagram", label: "Diagram", icon: Icon.diagram });
  // A code phase's plan and how it wrote from it (#81) live with its other details.
  const generation = row.handoff?.generation ?? null;
  if (fields.length || generation) views.push({ key: "details", label: "Details", icon: Icon.info });


  const [view, setView] = useState<ViewKey>(views[0]?.key ?? "summary");
  const active = views.find((v) => v.key === view) ?? views[0];
  // A finding's `path:line` was clicked: show this phase's files, on that one. Kept
  // here once taken — the parent forgets the ask, and the file browser still needs it.
  const [held, setHeld] = useState<FileFocus | undefined>(focus);
  useEffect(() => {
    if (!focus) return;
    setHeld(focus);
    if (files.some((f) => samePath(f.path, focus.path))) setView("files");
    onFocused?.();
    // Keyed on the request, not on `files`: a poll mustn't drag the view back.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.n]);
  // Dropped once the file browser has it (its effects run before this one): reopening
  // Files later must land on whatever the person chose, not on the old jump.
  useEffect(() => {
    if (held && view === "files") setHeld(undefined);
  }, [held, view]);

  if (views.length === 0) {
    return (
      <div className="artifact-empty">
        This agent returned no readable output. Send it back with a note and it will run again.
      </div>
    );
  }

  return (
    <div className="artifact">
      {/* Said here, next to the output it describes, rather than only as a badge on
          a row above. A phase that missed its shape is one whose keys nothing
          downstream could read — that is worth a sentence, not a tooltip. */}
      {row.schema_status === "invalid" && (
        <p className="artifact-flag" role="status">
          <strong>This doesn&apos;t match the shape {row.agent} declares.</strong>{" "}
          {row.schema_note ? `${row.schema_note}. ` : ""}
          Checks and later phases can&apos;t read it. Send it back to try again.
        </p>
      )}

      {/* Whether the code installs, builds and starts (#75): outcome first, the
          terminal folded under it — open when it failed. */}
      <BuildRunLine run={row.build_run} />
      {/* QA's tests, run for real (#76): what they measured, or why nothing did. */}
      <TestRunLine run={row.test_run} />
      {/* The scanners behind Warden's review (#77): which ran, or that none could. */}
      <ScanLine scan={row.scan} />

      {views.length > 1 && (
        <div className="artifact-bar">
          <div className="switcher" role="group" aria-label="What this agent produced">
            {views.map((v) => (
              <button
                key={v.key}
                className="seg-btn seg-btn-icon"
                aria-pressed={active?.key === v.key}
                onClick={() => setView(v.key)}
              >
                {v.icon}
                {v.label}
                {v.count !== undefined && <span className="seg-count">{v.count}</span>}
              </button>
            ))}
          </div>
          {files.length > 0 && <span className="artifact-note mono">{fileSummary(files)}</span>}
        </div>
      )}

      <div
        className={"artifact-view artifact-" + (active?.key ?? "summary")}
        style={{ maxHeight }}
      >
        {active?.key === "summary" && (
          <div className="artifact-pad">
            <Markdown>{row.content_md}</Markdown>
          </div>
        )}
        {active?.key === "files" && <FileBrowser files={files} focus={held} />}
        {active?.key === "diagram" && mermaid && (
          <div className="artifact-pad">
            <Mermaid source={mermaid} id={row.id} />
          </div>
        )}
        {active?.key === "details" && (
          <div className="artifact-pad artifact-details">
            {generation && <HowWritten generation={generation} />}
            {fields.length > 0 && <DetailFields fields={fields} />}
          </div>
        )}

      </div>

      <SkillsUsed row={row} />
      <WhatItSaw row={row} />
    </div>
  );
}

/**
 * The procedures this agent was actually working from.
 *
 * Provenance rather than content, so it sits under the work rather than over it.
 * Three states, and the third is the point: `null` is a phase from before the
 * library existed and says nothing, `[]` is a phase that got none — which is a real
 * thing to know, because selection happens before the model call and leaves no other
 * trace anywhere in the build.
 *
 * What `[]` deliberately does *not* claim is why. There are two causes — nothing in
 * the library matched this work, or what matched did not fit this model's share of
 * the window — and this row cannot tell them apart. Naming the first would send
 * someone off to add keywords that are already matching.
 */
function SkillsUsed({ row }: { row: PhaseResult }) {
  // Titles, as the Skills page and the composer print them. The row stores names,
  // which is right for the record and wrong for a person scanning the line.
  const titles = useSkillTitles();
  const used = row.skills_used;
  if (used === null || used === undefined) return null;
  return (
    <p className="artifact-skills">
      {used.length === 0 ? (
        <>
          <span className="artifact-skills-label">Worked from</span>
          <span
            className="dryrun-none"
            title={
              "No skill matched, or the matches didn't fit in this model's context. " +
              "Try the idea on the Skills page to see which."
            }
          >
            no skills
          </span>
        </>
      ) : (
        <>
          <span className="artifact-skills-label">Worked from</span>
          {used.map((name) => (
            <span key={name} className="pick">
              {titles[name] ?? name}
            </span>
          ))}
        </>
      )}
    </p>
  );
}

/** `files[7-19 of 19]` → `files 7–19 of 19`. Field names stay as written. */
function omittedText(omitted: string[]): string {
  return omitted
    .map((o) => o.replace(/^(\w+)\[(\d+)-(\d+) of (\d+)\]$/, (_m, k, a, b, n) =>
      a === b ? `${k} ${a} of ${n}` : `${k} ${a}–${b} of ${n}`,
    ))
    .map((o) => o.replace(/_/g, " "))
    .join(", ");
}

const STATE: Record<HandoffDep["full"], { label: string; tone: string }> = {
  whole: { label: "Everything", tone: "ok" },
  cut: { label: "Cut to fit", tone: "warn" },
  digest_only: { label: "Summary only", tone: "warn" },
};

/**
 * What this agent was handed by the phases before it (#80).
 *
 * Every hand-off carries a summary of the earlier work (paths, endpoints,
 * criteria); the full output follows when it fits this model's window. On a
 * small window it often doesn't, and a reviewer reading QA's tests or Warden's
 * findings needs to know the agent saw a cut copy. Folded away: it is
 * provenance, read when a result looks off, not on every pass.
 */
function WhatItSaw({ row }: { row: PhaseResult }) {
  const h = row.handoff;
  if (!h) return null;
  const deps = h.deps ?? [];
  const cut = deps.filter((d) => d.full !== "whole").length;
  const truncated = h.truncated_replies ?? 0;
  if (deps.length === 0 && !h.registry && truncated === 0) return null;

  const meta = [
    deps.length ? `${deps.length} hand-off${deps.length === 1 ? "" : "s"}` : null,
    cut ? `${cut} cut to fit` : deps.length ? "nothing cut" : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <details className="artifact-saw">
      <summary>
        <span className="artifact-saw-chev" aria-hidden>
          {Icon.chevron}
        </span>
        <span className="artifact-saw-title">What this agent saw</span>
        {meta && <span className="artifact-saw-meta">{meta}</span>}
        {truncated > 0 && (
          <span className="artifact-saw-tag warn">
            {truncated} {truncated === 1 ? "reply" : "replies"} hit the length limit
          </span>
        )}
      </summary>
      <div className="artifact-saw-body">
        {deps.length > 0 && (
          <ul className="artifact-saw-list">
            {deps.map((d) => {
              const who = AGENT_BY_KEY[d.phase];
              const state = STATE[d.full] ?? STATE.whole;
              return (
                <li key={d.phase} className="artifact-saw-row">
                  <span className="artifact-saw-who">
                    <span className="artifact-saw-code">{who?.codename ?? d.phase}</span>
                    <span className="artifact-saw-role">{who?.role ?? ""}</span>
                  </span>
                  <span className="artifact-saw-what">
                    <span className={"artifact-saw-tag " + state.tone}>{state.label}</span>
                    {d.full === "cut" && d.omitted.length > 0 && (
                      <span className="artifact-saw-left">Left out: {omittedText(d.omitted)}</span>
                    )}
                  </span>
                </li>
              );
            })}
          </ul>
        )}
        <p className="artifact-saw-note">
          {deps.length > 0 && "Each hand-off includes a summary of the earlier work. "}
          {h.registry
            ? "Held to the names System Design chose."
            : deps.length > 0
              ? "No shared names yet."
              : ""}
          {truncated > 0 &&
            " The rest of a cut-off reply was asked for separately."}
        </p>
      </div>
    </details>
  );
}
