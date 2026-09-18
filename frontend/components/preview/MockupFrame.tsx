"use client";

import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import type { PreviewRoute } from "@/lib/api";

/**
 * The rendered mockup, in a browser frame, in a sandbox it cannot escape.
 *
 * The mockup is a small site now — pages, a router, forms that store — so the frame
 * behaves like a browser around it: the address bar shows the page the prototype is
 * on, and the page tabs move it. Both follow the prototype's own runtime, which
 * reports every navigation up by `postMessage`; nothing here reloads the document to
 * change page, so records added on one page are still there on the next.
 *
 * Two modes, and the difference is who gets the click. In **use** mode the prototype
 * does: links route, forms validate and store, lists filter. In **edit** mode the
 * Preview tab does: a click selects the section under it for a change request. The
 * mode is sent to the frame rather than baked into it, so switching never reloads.
 */

// Injected into every document the frame shows. The frame is `allow-scripts
// allow-forms` without same-origin, so nothing in it can reach the app — but a srcdoc
// frame resolves a relative link against the *parent's* URL, and following one loads
// the app itself inside the frame. This keeps any document, old single-page mockups
// included, from navigating away; the prototype's own runtime still handles the click.
const GUARD_SCRIPT = `
<script>
(function(){
  window.addEventListener('click', function(e){
    var a = e.target && e.target.closest && e.target.closest('a[href]');
    if (a) e.preventDefault();
  }, true);
  window.addEventListener('submit', function(e){ e.preventDefault(); }, true);
})();
</script>`;

// Section selection for the edit loop. Listens for its mode, reports clicks up, and
// accepts "highlight" messages back — revealing the section's page first when it is
// on one that is not showing.
const SELECT_SCRIPT = `
<script>
(function(){
  var mode = 'use';
  var HL = '2px solid #f5a524';
  function clearAll(){ document.querySelectorAll('[data-section]').forEach(function(s){ s.style.removeProperty('outline'); s.style.removeProperty('outline-offset'); s.style.removeProperty('cursor'); }); }
  function outline(el){ clearAll(); if(el){ el.style.outline=HL; el.style.outlineOffset='-2px'; } }
  function byId(id){ try { return id ? document.querySelector('[data-section="'+(window.CSS&&CSS.escape?CSS.escape(id):id)+'"]') : null; } catch(_) { return null; } }
  document.addEventListener('mouseover', function(e){
    if (mode !== 'edit') return;
    var t=e.target; if(t&&t.closest){ var el=t.closest('[data-section]'); if(el) el.style.cursor='pointer'; }
  });
  window.addEventListener('click', function(e){
    if (mode !== 'edit') return;
    var t=e.target; if(!t||!t.closest) return;
    var el=t.closest('[data-section]'); if(!el) return;
    e.preventDefault(); e.stopPropagation(); outline(el);
    parent.postMessage({__preview:true, type:'select', id: el.getAttribute('data-section'), label: el.getAttribute('data-label')||el.getAttribute('data-section')}, '*');
  }, true);
  window.addEventListener('message', function(e){
    var d=e.data; if(!d||!d.__preview) return;
    if (d.type==='mode') { mode = d.mode === 'edit' ? 'edit' : 'use'; if (mode==='use') clearAll(); }
    if (d.type==='highlight') {
      var el = byId(d.id);
      if (el && window.__app && window.__app.reveal) window.__app.reveal(el);
      outline(el);
    }
  });
})();
</script>`;

function inject(html: string, scripts: string): string {
  return html.includes("</body>") ? html.replace("</body>", scripts + "</body>") : html + scripts;
}

export type MockupFrameHandle = {
  /** Outline the section, switching to its page first when it is on another. */
  highlight: (sectionId: string | null) => void;
  /** Whether a message came from this frame's document rather than any other window
   *  that can post to the page. (Script inside the frame is kept out by the server:
   *  model-written handlers are stripped before a section is saved.) */
  owns: (source: MessageEventSource | null) => boolean;
};

type Props = {
  html: string;
  /** Clickable section selection, for the Preview tab's edit loop. */
  selectable?: boolean;
  /** Who gets the click: the prototype ("use") or the section picker ("edit"). */
  mode?: "use" | "edit";
  /** The prototype's pages, for the tabs in the frame's toolbar. */
  routes?: PreviewRoute[];
  /** Frame height in px. The review pane runs shorter than the editor. */
  height?: number;
  host?: string;
  onLoad?: () => void;
};

const MockupFrame = forwardRef<MockupFrameHandle, Props>(function MockupFrame(
  { html, selectable = false, mode = "use", routes = [], height, host = "localhost:3000", onLoad },
  ref,
) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const first = routes[0]?.path ?? "/";
  const [path, setPath] = useState<string>(first);
  const srcDoc = useMemo(
    () => inject(html, GUARD_SCRIPT + (selectable ? SELECT_SCRIPT : "")),
    [html, selectable],
  );

  const post = useCallback((message: Record<string, unknown>) => {
    frameRef.current?.contentWindow?.postMessage({ __preview: true, ...message }, "*");
  }, []);

  useImperativeHandle(
    ref,
    () => ({
      highlight: (sectionId) => post({ type: "highlight", id: sectionId }),
      owns: (source) => Boolean(source) && source === frameRef.current?.contentWindow,
    }),
    [post],
  );

  // The prototype reports its own navigation — a link inside it, a form that
  // redirects — so the address bar and tabs follow it, not the other way round.
  useEffect(() => {
    function onMessage(e: MessageEvent) {
      if (e.source !== frameRef.current?.contentWindow) return;
      const d = e.data;
      if (d && d.__preview && d.type === "route" && typeof d.path === "string") setPath(d.path);
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  useEffect(() => {
    if (selectable) post({ type: "mode", mode });
  }, [mode, selectable, post]);

  // A new document starts on its first page.
  useEffect(() => {
    setPath(first);
  }, [html, first]);

  const go = (target: string) => {
    setPath(target);
    post({ type: "go", path: target });
  };

  const address = routes.length ? `${host}/#${path}` : host;

  return (
    <div className="prev-frame">
      <div className="prev-chrome">
        <span className="dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="prev-url" title={address}>
          {address}
        </span>
        {routes.length > 1 && (
          <nav className="prev-pages" aria-label="Mockup pages">
            {routes.map((r) => (
              <button
                key={r.path}
                type="button"
                className="prev-page"
                aria-current={r.path === path ? "page" : undefined}
                onClick={() => go(r.path)}
                title={`${r.title} — #${r.path}`}
              >
                {r.title}
              </button>
            ))}
          </nav>
        )}
      </div>
      <iframe
        ref={frameRef}
        title="Generated mockup"
        sandbox="allow-scripts allow-forms"
        srcDoc={srcDoc}
        style={height ? { height } : undefined}
        onLoad={() => {
          if (selectable) post({ type: "mode", mode });
          onLoad?.();
        }}
      />
    </div>
  );
});

export default MockupFrame;
