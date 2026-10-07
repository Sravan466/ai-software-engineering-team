"use client";

import type { Scan, ScanToolName } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { plural } from "@/lib/text";

/**
 * The security scanners behind Warden's review (#77), as one line on its card.
 *
 * "semgrep 1.139.0 · bandit 1.8.6 · npm audit 10.8.2 · 14.4 s" and how many findings
 * they reported — folded under it, each tool with its version, what it found, or why
 * it didn't run. With no scanner at all the line says so in the warning style, because
 * then every finding on this card is the model's opinion: the one fact a reviewer must
 * not have to infer.
 */
const ORDER: ScanToolName[] = ["semgrep", "bandit", "npm audit", "pip-audit"];

const WHAT: Record<ScanToolName, string> = {
  semgrep: "Code patterns, OWASP Top 10, secrets",
  bandit: "Python security checks",
  "npm audit": "JavaScript dependencies",
  "pip-audit": "Python dependencies",
};

export default function ScanLine({ scan }: { scan: Scan | null | undefined }) {
  if (!scan) return null;
  const skipped = scan.status === "skipped";
  const found = scan.findings?.length ?? 0;
  const tools = ORDER.filter((t) => scan.tools[t]);
  const ran = tools.filter((t) => scan.tools[t]?.status === "ran");

  if (skipped) {
    return (
      <div className="scanline" data-state="skipped" role="status">
        <div className="scanline-head">
          <span className="scanline-mark" aria-hidden="true">{Icon.alert}</span>
          <span className="scanline-title">Scanners</span>
          <span className="scanline-summary">
            Not available: the findings below are the model&apos;s opinion.
            {scan.reason ? <span className="scanline-why"> {scan.reason}</span> : null}
          </span>
        </div>
      </div>
    );
  }

  const versions = ran
    .map((t) => (scan.tools[t]?.version ? `${t} ${scan.tools[t]?.version}` : t))
    .concat(`${scan.seconds.toFixed(1)} s`)
    .join(" · ");

  return (
    <details className="scanline" data-state={found ? "found" : "clean"}>
      <summary>
        <span className="scanline-chev" aria-hidden="true">{Icon.chevron}</span>
        <span className="scanline-mark" aria-hidden="true">{found ? Icon.search : Icon.check}</span>
        <span className="scanline-title">Scanners</span>
        <span className="scanline-summary">{versions}</span>
        <span className="scanline-count">
          {found ? plural(found, "finding") : "nothing reported"}
          {scan.truncated ? " (more not shown)" : ""}
        </span>
        <span className="scanline-where">Docker sandbox · no code run</span>
      </summary>

      <div className="scanline-body">
        <ul className="scanline-tools">
          {tools.map((t) => {
            const tool = scan.tools[t]!;
            return (
              <li key={t} className="scanline-tool" data-status={tool.status}>
                <span className="scanline-tool-name">
                  {t}
                  {tool.version && <span className="scanline-tool-ver mono">{tool.version}</span>}
                </span>
                <span className="scanline-tool-what">{WHAT[t]}</span>
                <span className="scanline-tool-state">
                  {tool.status === "ran"
                    ? tool.count
                      ? plural(tool.count, "finding")
                      : "nothing found"
                    : tool.status === "not_needed"
                      ? tool.reason ?? "Not needed here."
                      : `Didn't run: ${tool.reason ?? "no reason given."}`}
                </span>
              </li>
            );
          })}
        </ul>
        {scan.rules && <p className="scanline-note">{scan.rules}</p>}
        <p className="scanline-note">
          A finding is fixed when its tool, run again on the fixed code, no longer reports it at that line.
        </p>
      </div>
    </details>
  );
}
