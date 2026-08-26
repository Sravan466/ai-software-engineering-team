"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, PreviewSection, PreviewState } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";

// Injected into the sandboxed iframe (sandbox="allow-scripts", no same-origin) so model HTML
// can never reach the parent. It reports clicks up via postMessage and accepts "highlight"
// messages back, so selection never has to change `srcDoc` (which would reload the document).
const SELECT_SCRIPT = `
<script>
(function(){
  var HL='2px solid #f5a524';
  function clearAll(){ document.querySelectorAll('[data-section]').forEach(function(s){ s.style.removeProperty('outline'); s.style.removeProperty('outline-offset'); }); }
  function outline(el){ clearAll(); if(el){ el.style.outline=HL; el.style.outlineOffset='-2px'; } }
  function byId(id){ try { return id ? document.querySelector('[data-section="'+(window.CSS&&CSS.escape?CSS.escape(id):id)+'"]') : null; } catch(_) { return null; } }
  document.addEventListener('mouseover', function(e){ var t=e.target; if(t&&t.closest){ var el=t.closest('[data-section]'); if(el) el.style.cursor='pointer'; } });
  document.addEventListener('click', function(e){
    var t=e.target; if(!t||!t.closest) return;
    var el=t.closest('[data-section]'); if(!el) return;
    e.preventDefault(); e.stopPropagation(); outline(el);
    parent.postMessage({__preview:true, type:'select', id: el.getAttribute('data-section'), label: el.getAttribute('data-label')||el.getAttribute('data-section')}, '*');
  }, true);
  window.addEventListener('message', function(e){ var d=e.data; if(d&&d.__preview&&d.type==='highlight') outline(byId(d.id)); });
})();
</script>`;

function buildSrcDoc(html: string): string {
  return html.includes("</body>")
    ? html.replace("</body>", SELECT_SCRIPT + "</body>")
    : html + SELECT_SCRIPT;
}

export default function VisualPreview({ id }: { id: string }) {
  const [state, setState] = useState<PreviewState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<PreviewSection | null>(null);
  const [instruction, setInstruction] = useState("");
  const iframeRef = useRef<HTMLIFrameElement>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setState(await api.getPreview(id));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Selection clicks coming up from the sandboxed iframe.
  useEffect(() => {
    function onMsg(e: MessageEvent) {
      const d = e.data;
      if (d && d.__preview && d.type === "select") setSelected({ id: d.id, label: d.label });
    }
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, []);

  // Push the current selection into the iframe (on select, and after each (re)load).
  const highlight = useCallback((sectionId: string | null) => {
    iframeRef.current?.contentWindow?.postMessage(
      { __preview: true, type: "highlight", id: sectionId },
      "*",
    );
  }, []);
  useEffect(() => {
    highlight(selected?.id ?? null);
  }, [selected, highlight]);

  async function run(fn: () => Promise<PreviewState>, clearSelection: boolean) {
    setBusy(true);
    setError("");
    try {
      setState(await fn());
      if (clearSelection) setSelected(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const generate = () => run(() => api.generatePreview(id), true);
  const undo = () => run(() => api.undoPreview(id), true);
  async function applyEdit() {
    if (!selected || !instruction.trim()) return;
    await run(() => api.editPreviewSection(id, selected.id, instruction.trim()), false);
    setInstruction("");
  }

  const html = state?.html ?? null;
  const srcDoc = useMemo(() => (html ? buildSrcDoc(html) : ""), [html]);

  if (loading && !state) {
    return (
      <div className="card">
        <SkeletonLines lines={3} />
      </div>
    );
  }

  const errorNotice = error ? (
    <div className="notice notice-bad" role="alert">
      {Icon.alert}
      <div className="notice-body">
        <span className="notice-text">{error}</span>
      </div>
    </div>
  ) : null;

  // Empty state — nothing generated yet. It teaches what the tab is for.
  if (!html) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        <div className="card empty">
          <h3>See it before it exists</h3>
          <p>
            Generate a clickable mockup of what these agents are building. Select any section in the
            result and describe the change you want in plain language.
            {state && !state.has_frontend && " It gets much sharper once the Frontend phase has run."}
          </p>
          <button className="btn btn-primary" disabled={busy} onClick={generate}>
            {busy && <span className="btn-spinner" aria-hidden="true" />}
            {busy ? "Generating…" : "Generate mockup"}
            {!busy && Icon.sparkle}
          </button>
          {busy && (
            <p className="field-hint" style={{ margin: 0 }}>
              A local model usually takes 30–60 seconds for this.
            </p>
          )}
        </div>
        {errorNotice}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div className="prev-toolbar">
        <h3 className="label">Mockup · approximate</h3>
        <span className="rule" />
        <span className="badge badge-mono">
          {state!.revisions.length} rev{state!.revisions.length === 1 ? "" : "s"}
        </span>
        <button
          className="btn btn-sm"
          disabled={busy || state!.revisions.length === 0}
          onClick={undo}
        >
          {Icon.undo} Undo
        </button>
        <button className="btn btn-sm" disabled={busy} onClick={generate}>
          {busy ? <span className="btn-spinner" aria-hidden="true" /> : Icon.refresh}
          Regenerate
        </button>
      </div>

      {busy && (
        <p className="field-hint" aria-live="polite">
          Working… a local model usually takes 30–60 seconds.
        </p>
      )}
      {errorNotice}

      <div className="prev-frame">
        <div className="prev-chrome">
          <span className="dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span className="prev-url">localhost:3000</span>
        </div>
        <iframe
          ref={iframeRef}
          key={state!.revisions[0]?.id || "iframe"}
          title="Generated mockup"
          sandbox="allow-scripts"
          srcDoc={srcDoc}
          onLoad={() => highlight(selected?.id ?? null)}
        />
      </div>

      {selected ? (
        <div className="card" style={{ borderColor: "var(--accent-line)", background: "var(--warn-soft)" }}>
          <div className="sec-head">
            <h3 className="label" style={{ color: "var(--accent)" }}>
              Editing
            </h3>
            <span style={{ fontSize: "var(--t-base)", fontWeight: 600 }}>{selected.label}</span>
            <span className="rule" />
          </div>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 10, flexWrap: "wrap" }}>
            <div className="field" style={{ flex: 1, minWidth: 240 }}>
              <label htmlFor="prev-instruction">What should change?</label>
              <input
                id="prev-instruction"
                className="input"
                placeholder="e.g. make the headline bigger and add a Get started button"
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") applyEdit();
                }}
              />
            </div>
            <button
              className="btn btn-accent"
              disabled={busy || !instruction.trim()}
              onClick={applyEdit}
            >
              {busy && <span className="btn-spinner" aria-hidden="true" />}
              Apply change
            </button>
            <button
              className="btn"
              disabled={busy}
              onClick={() => {
                setSelected(null);
                setInstruction("");
              }}
            >
              Done
            </button>
          </div>
        </div>
      ) : (
        <p className="field-hint">
          Click any section in the mockup to select it, then describe the change you want.
        </p>
      )}

      {state!.sections.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 7 }}>
          {state!.sections.map((s) => (
            <button
              key={s.id}
              className={"badge" + (selected?.id === s.id ? " badge-warn" : "")}
              style={{ cursor: "pointer" }}
              onClick={() => setSelected(s)}
            >
              {s.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
