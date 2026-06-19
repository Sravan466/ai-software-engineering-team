"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, PreviewSection, PreviewState } from "@/lib/api";

// Injected into the sandboxed iframe (sandbox="allow-scripts", no same-origin) so model HTML
// can never reach the parent. It reports clicks up via postMessage and accepts "highlight"
// messages back, so selection never has to change `srcDoc` (which would reload the document).
const SELECT_SCRIPT = `
<script>
(function(){
  var HL='2px solid #e5484d';
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
    iframeRef.current?.contentWindow?.postMessage({ __preview: true, type: "highlight", id: sectionId }, "*");
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

  if (loading && !state) return <p className="fd-dim text-sm">Loading preview…</p>;

  // Empty state — no preview generated yet.
  if (!html) {
    return (
      <div className="fd-card space-y-3">
        <p className="fd-kicker">visual preview</p>
        <p className="text-sm fd-muted">
          Generate a live visual mockup of what you&apos;re building. You&apos;ll be able to click any
          section and ask for changes in plain language.
          {state && !state.has_frontend && " It gets richer once the Frontend phase has run."}
        </p>
        <button className="fd-btn fd-btn-primary" disabled={busy} onClick={generate}>
          {busy ? "Generating… (local models can take 30–60s)" : "Generate visual preview →"}
        </button>
        {error && (
          <p className="text-sm" style={{ color: "var(--accent)" }}>
            {error}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="fd-kicker">visual preview · approximate mockup</span>
        <span className="fd-rule flex-1" />
        <span className="fd-badge fd-badge-muted">{state!.revisions.length} rev</span>
        <button className="fd-btn" disabled={busy || state!.revisions.length === 0} onClick={undo}>
          Undo
        </button>
        <button className="fd-btn" disabled={busy} onClick={generate}>
          Regenerate
        </button>
      </div>

      {busy && <p className="text-xs fd-dim">Working… local models can take 30–60s.</p>}
      {error && (
        <p className="text-sm" style={{ color: "var(--accent)" }}>
          {error}
        </p>
      )}

      <div className="fd-card" style={{ padding: 0, overflow: "hidden" }}>
        <iframe
          ref={iframeRef}
          key={state!.revisions[0]?.id || "iframe"}
          title="Visual preview"
          sandbox="allow-scripts"
          srcDoc={srcDoc}
          onLoad={() => highlight(selected?.id ?? null)}
          style={{ width: "100%", height: 620, border: 0, background: "#fff", display: "block" }}
        />
      </div>

      {selected ? (
        <div className="fd-card fd-card-accent space-y-2">
          <p className="fd-kicker" style={{ color: "var(--accent)" }}>
            editing section
          </p>
          <h3 className="fd-title">{selected.label}</h3>
          <div className="flex flex-wrap items-center gap-2">
            <input
              className="fd-input flex-1"
              placeholder="Describe the change… e.g. 'make the headline bigger and add a Get Started button'"
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") applyEdit();
              }}
            />
            <button
              className="fd-btn fd-btn-primary"
              disabled={busy || !instruction.trim()}
              onClick={applyEdit}
            >
              Apply →
            </button>
            <button
              className="fd-btn"
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
        <p className="text-sm fd-dim">
          Click any section in the preview to select it, then describe the change you want.
        </p>
      )}

      {state!.sections.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {state!.sections.map((s) => (
            <button
              key={s.id}
              className={`fd-badge ${selected?.id === s.id ? "fd-badge-accent" : "fd-badge-muted"}`}
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
