"use client";

import { useMemo, useState } from "react";
import type { PhaseResult } from "@/lib/api";
import Markdown from "@/components/ui/Markdown";
import { Icon } from "@/components/shell/icons";
import DetailFields from "./DetailFields";
import FileBrowser from "./FileBrowser";
import Mermaid from "./Mermaid";
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
}: {
  row: PhaseResult;
  maxHeight?: number;
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
  if (fields.length) views.push({ key: "details", label: "Details", icon: Icon.info });

  const [view, setView] = useState<ViewKey>(views[0]?.key ?? "summary");
  const active = views.find((v) => v.key === view) ?? views[0];

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
          Checks and later phases that read these keys have nothing to read. Send it
          back and the agent will try again.
        </p>
      )}

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
        {active?.key === "files" && <FileBrowser files={files} />}
        {active?.key === "diagram" && mermaid && (
          <div className="artifact-pad">
            <Mermaid source={mermaid} id={row.id} />
          </div>
        )}
        {active?.key === "details" && (
          <div className="artifact-pad">
            <DetailFields fields={fields} />
          </div>
        )}
      </div>

      <SkillsUsed row={row} />
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
              "Either nothing in the library matched this work, or what matched did " +
              "not fit this model's share of the context window. Try the idea on the " +
              "Skills page to see which."
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
              {name}
            </span>
          ))}
        </>
      )}
    </p>
  );
}
