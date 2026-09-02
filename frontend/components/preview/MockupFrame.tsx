"use client";

import { forwardRef, useMemo } from "react";

/**
 * The rendered mockup, in a browser frame, in a sandbox it cannot escape.
 *
 * Two callers with two different jobs: the Ship review shows the picture beside the
 * decision, and the Preview tab makes it clickable so a section can be edited. The
 * selection script is injected only for the second — a reviewer deciding whether to
 * ship should not have the page snatching their clicks.
 */

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

function withSelection(html: string): string {
  return html.includes("</body>")
    ? html.replace("</body>", SELECT_SCRIPT + "</body>")
    : html + SELECT_SCRIPT;
}

type Props = {
  html: string;
  /** Clickable section selection, for the Preview tab's edit loop. */
  selectable?: boolean;
  /** Frame height in px. The review pane runs shorter than the editor. */
  height?: number;
  url?: string;
  onLoad?: () => void;
};

const MockupFrame = forwardRef<HTMLIFrameElement, Props>(function MockupFrame(
  { html, selectable = false, height, url = "localhost:3000", onLoad },
  ref,
) {
  const srcDoc = useMemo(() => (selectable ? withSelection(html) : html), [html, selectable]);

  return (
    <div className="prev-frame">
      <div className="prev-chrome">
        <span className="dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="prev-url">{url}</span>
      </div>
      <iframe
        ref={ref}
        title="Generated mockup"
        sandbox="allow-scripts"
        srcDoc={srcDoc}
        style={height ? { height } : undefined}
        onLoad={onLoad}
      />
    </div>
  );
});

export default MockupFrame;
