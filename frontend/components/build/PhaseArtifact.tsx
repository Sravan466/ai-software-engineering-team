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
    </div>
  );
}
