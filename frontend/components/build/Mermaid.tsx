"use client";

import { useEffect, useRef, useState } from "react";
import CodeBlock from "@/components/preview/CodeBlock";
import { Icon } from "@/components/shell/icons";

/**
 * Draws the System Design agent's `architecture_diagram_mermaid`.
 *
 * The agent has always produced this and it has never been rendered — a reviewer
 * approving an architecture was shown prose about a diagram that existed in the
 * payload the whole time.
 *
 * Two things this has to survive:
 *
 *   • **Invalid definitions.** A 7B model writing Mermaid gets it wrong regularly.
 *     `mermaid.parse` throws before anything is drawn, so a bad diagram degrades to
 *     its source with an explanation, never to a blank panel or a thrown render.
 *   • **The theme.** Mermaid bakes colours into the SVG it emits, so it can't inherit
 *     from CSS. The Foundry tokens are read off the document at render time, which
 *     keeps one source of truth rather than a second hard-coded palette here.
 */

function token(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

type State =
  | { phase: "loading" }
  | { phase: "drawn"; svg: string }
  | { phase: "invalid"; reason: string };

export default function Mermaid({ source, id }: { source: string; id: string }) {
  const [state, setState] = useState<State>({ phase: "loading" });
  const [showSource, setShowSource] = useState(false);
  const holder = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;

    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          // `strict` runs the emitted SVG through DOMPurify — this is model output.
          securityLevel: "strict",
          theme: "base",
          fontFamily: token("--sans", "system-ui, sans-serif"),
          themeVariables: {
            darkMode: true,
            background: token("--bg-sunken", "#07080c"),
            primaryColor: token("--bg-raised", "#191d26"),
            primaryTextColor: token("--ink", "#e9ecf2"),
            primaryBorderColor: token("--line-3", "#3a4152"),
            secondaryColor: token("--bg-panel", "#12151c"),
            tertiaryColor: token("--bg-panel", "#12151c"),
            lineColor: token("--ink-4", "#646b7b"),
            textColor: token("--ink-2", "#aeb4c2"),
            mainBkg: token("--bg-raised", "#191d26"),
            nodeBorder: token("--line-3", "#3a4152"),
            clusterBkg: token("--bg-panel", "#12151c"),
            clusterBorder: token("--line-2", "#2a303d"),
            edgeLabelBackground: token("--bg-sunken", "#07080c"),
            fontSize: "13px",
          },
        });

        // Throws on a malformed definition, before anything reaches the DOM.
        await mermaid.parse(source);
        const { svg } = await mermaid.render(`mermaid-${id}`, source);
        if (live) setState({ phase: "drawn", svg });
      } catch (e: unknown) {
        const reason = e instanceof Error ? e.message : "The diagram definition is malformed.";
        if (live) setState({ phase: "invalid", reason });
      }
    })();

    return () => {
      live = false;
    };
  }, [source, id]);

  useEffect(() => {
    if (state.phase === "drawn" && holder.current) holder.current.innerHTML = state.svg;
  }, [state]);

  if (state.phase === "loading") {
    return (
      <div className="diagram-frame diagram-loading" role="status">
        <span className="btn-spinner" aria-hidden="true" />
        <span>Drawing the architecture diagram…</span>
      </div>
    );
  }

  if (state.phase === "invalid") {
    return (
      <div className="diagram-invalid">
        <div className="notice notice-warn">
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">This diagram couldn&apos;t be drawn</span>
            <span className="notice-text">
              The agent&apos;s Mermaid definition is malformed, so it&apos;s shown as source
              instead. Sending the phase back with &ldquo;fix the architecture diagram&rdquo;
              usually gets a valid one. Reported: {state.reason.split("\n")[0]}
            </span>
          </div>
        </div>
        <CodeBlock code={source} path="architecture.mmd" tag="text" />
      </div>
    );
  }

  return (
    <div className="diagram">
      <div className="diagram-frame">
        {/* mermaid returns a sanitized SVG string; there is no element API to build. */}
        <div className="diagram-svg" ref={holder} />
      </div>
      <div className="diagram-foot">
        <button
          className="btn btn-sm btn-ghost"
          aria-expanded={showSource}
          onClick={() => setShowSource((v) => !v)}
        >
          <span className={"phase-chev" + (showSource ? " open" : "")} aria-hidden="true">
            {Icon.chevron}
          </span>
          {showSource ? "Hide definition" : "Show definition"}
        </button>
      </div>
      {showSource && <CodeBlock code={source} path="architecture.mmd" tag="text" />}
    </div>
  );
}
