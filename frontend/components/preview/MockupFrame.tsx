"use client";

import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { PatchOp, PreviewRoute } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { BRIDGE_SCRIPT } from "./bridge";

/**
 * The rendered mockup, in a browser frame, in a sandbox it cannot escape.
 *
 * The mockup is a small site — pages, a router, forms that store — so the frame
 * behaves like a browser around it: the address bar shows the page the prototype is
 * on and takes a new one, and the page tabs move it. Both follow the prototype's own
 * runtime, which reports every navigation up by `postMessage`; nothing here reloads
 * the document to change page, so records added on one page are still there on the
 * next.
 *
 * With `stage`, the frame is the Preview tab's full canvas: the site renders at a real
 * device width (desktop 1280, tablet 768, mobile 390, or dragged to any width) and is
 * scaled to fit when the column is narrower, so `xl:` layouts render as they would on
 * a laptop. It can go fullscreen, open in its own tab, and reload. Without `stage` it
 * is the plain framed picture the Ship review shows.
 *
 * Two modes, and the difference is who gets the click. In **use** mode the prototype
 * does. In **edit** mode the bridge (`bridge.ts`) does: hover shows what a click will
 * pick, and the pick is reported up. The mode is sent to the frame rather than baked
 * into it, so switching never reloads.
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

function inject(html: string, scripts: string): string {
  return html.includes("</body>") ? html.replace("</body>", scripts + "</body>") : html + scripts;
}

export type Device = "desktop" | "tablet" | "mobile" | "custom";
export const DEVICE_WIDTH: Record<Exclude<Device, "custom">, number> = { desktop: 1280, tablet: 768, mobile: 390 };
const MIN_W = 320;
const MAX_W = 1920;

export type MockupFrameHandle = {
  /** Select the section, switching to its page first when it is on another. */
  highlight: (sectionId: string | null) => void;
  /** Select one element by id; `reveal` scrolls to it and switches page. */
  select: (oid: string | null, reveal?: boolean) => void;
  /** Show changes at once, before they are saved. */
  apply: (ops: PatchOp[]) => void;
  /** Start typing into the selected element. */
  editText: () => void;
  /** Whether a message came from this frame's document rather than any other window
   *  that can post to the page. (Script inside the frame is kept out by the server:
   *  model-written handlers are stripped before a section is saved.) */
  owns: (source: MessageEventSource | null) => boolean;
};

type Props = {
  html: string;
  /** The element selector, for the Preview tab's edit loop. */
  selectable?: boolean;
  /** Who gets the click: the prototype ("use") or the selector ("edit"). */
  mode?: "use" | "edit";
  /** The prototype's pages, for the tabs in the frame's toolbar. */
  routes?: PreviewRoute[];
  /** Frame height in px, for the plain (non-stage) frame. */
  height?: number;
  /** The full canvas: device sizes, fit, fullscreen, open in a tab, reload. */
  stage?: boolean;
  host?: string;
  /** Called after every load — a new revision, a reload — once the page is restored. */
  onLoad?: () => void;
};

const MockupFrame = forwardRef<MockupFrameHandle, Props>(function MockupFrame(
  { html, selectable = false, mode = "use", routes = [], height, stage = false, host = "localhost:3000", onLoad },
  ref,
) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const shellRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const first = routes[0]?.path ?? "/";
  const [path, setPath] = useState<string>(first);
  const [draft, setDraft] = useState<string | null>(null);
  const [device, setDevice] = useState<Device>("desktop");
  const [width, setWidth] = useState<number>(DEVICE_WIDTH.desktop);
  const [fit, setFit] = useState(true);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const [full, setFull] = useState(false);
  const [reloads, setReloads] = useState(0);
  const pathRef = useRef(path);
  pathRef.current = path;

  const srcDoc = useMemo(
    () => inject(html, GUARD_SCRIPT + (selectable ? BRIDGE_SCRIPT : "")),
    [html, selectable],
  );

  const post = useCallback((message: Record<string, unknown>) => {
    frameRef.current?.contentWindow?.postMessage({ __preview: true, ...message }, "*");
  }, []);

  useImperativeHandle(
    ref,
    () => ({
      highlight: (sectionId) => post({ type: "highlight", id: sectionId }),
      select: (oid, reveal = true) => post(oid ? { type: "select", oid, reveal } : { type: "clear" }),
      apply: (ops) => post({ type: "ops", ops }),
      editText: () => post({ type: "editText" }),
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

  // A new revision keeps the page you were on, as long as the site still has it.
  useEffect(() => {
    if (!routes.some((r) => r.path === pathRef.current)) setPath(first);
  }, [routes, first]);

  const go = (target: string) => {
    const clean = "/" + target.replace(/^[#/]+/, "").trim();
    setPath(clean);
    post({ type: "go", path: clean });
  };

  // ── sizing ────────────────────────────────────────────────────────────────
  useLayoutEffect(() => {
    if (!stage || !stageRef.current) return;
    const el = stageRef.current;
    const measure = () => setBox({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [stage]);

  useEffect(() => {
    const onChange = () => setFull(document.fullscreenElement === shellRef.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const gutter = 32;
  const room = Math.max(box.w - gutter, 1);
  const scale = stage && fit && box.w ? Math.min(1, room / width) : 1;
  const frameH = stage && box.h ? Math.round((box.h - 24) / scale) : undefined;

  function pickDevice(next: Exclude<Device, "custom">) {
    setDevice(next);
    setWidth(DEVICE_WIDTH[next]);
  }

  // Drag the frame's edge to any width, in real (unscaled) pixels.
  function startResize(e: React.PointerEvent<HTMLDivElement>) {
    e.preventDefault();
    const startX = e.clientX;
    const startW = width;
    const target = e.currentTarget;
    target.setPointerCapture(e.pointerId);
    // Both edges move when the frame is centred, so a pixel of drag is two of width.
    const move = (ev: PointerEvent) => {
      const next = Math.round(Math.min(MAX_W, Math.max(MIN_W, startW + ((ev.clientX - startX) * 2) / scale)));
      setWidth(next);
      setDevice("custom");
    };
    const up = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
  }
  function resizeKey(e: React.KeyboardEvent<HTMLDivElement>) {
    const by = e.shiftKey ? 80 : 16;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      setWidth((w) => Math.min(MAX_W, Math.max(MIN_W, w + (e.key === "ArrowRight" ? by : -by))));
      setDevice("custom");
    }
  }

  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await shellRef.current?.requestFullscreen();
    } catch {
      /* the browser refused — nothing to undo */
    }
  }

  // Its own tab, still sandboxed: the page is a wrapper with no script of its own
  // around the same sandboxed frame, so the mockup never runs as this app's origin.
  function openInTab() {
    const doc = inject(html, GUARD_SCRIPT);
    const escaped = doc.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
    const title = (html.match(/<title>([^<]*)<\/title>/i)?.[1] ?? "Mockup").replace(/[<"]/g, "");
    const wrapper =
      `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">` +
      `<title>${title} — mockup</title><style>html,body{margin:0;height:100%;background:#fff}iframe{border:0;width:100%;height:100%;display:block}</style></head>` +
      `<body><iframe sandbox="allow-scripts allow-forms" title="${title}" srcdoc="${escaped}"></iframe></body></html>`;
    const url = URL.createObjectURL(new Blob([wrapper], { type: "text/html" }));
    window.open(url, "_blank", "noopener");
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  const address = routes.length ? `#${draft ?? path}` : host;

  const onFrameLoad = () => {
    if (selectable) post({ type: "mode", mode });
    // Back to the page you were on: a new revision or a reload starts at home.
    if (routes.length && pathRef.current && pathRef.current !== first) post({ type: "go", path: pathRef.current });
    onLoad?.();
  };

  const iframe = (
    <iframe
      key={reloads}
      ref={frameRef}
      title="Generated mockup"
      sandbox="allow-scripts allow-forms"
      srcDoc={srcDoc}
      style={
        stage
          ? { width, height: frameH, transform: scale !== 1 ? `scale(${scale})` : undefined, transformOrigin: "0 0" }
          : height
            ? { height }
            : undefined
      }
      onLoad={onFrameLoad}
    />
  );

  return (
    <div className={"prev-frame" + (stage ? " prev-frame-stage" : "") + (full ? " is-full" : "")} ref={shellRef}>
      <div className="prev-chrome">
        <span className="dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        {stage && routes.length ? (
          <form
            className="prev-url prev-url-edit"
            onSubmit={(e) => {
              e.preventDefault();
              if (draft !== null) go(draft);
              setDraft(null);
              (e.currentTarget.querySelector("input") as HTMLInputElement | null)?.blur();
            }}
          >
            <span className="prev-host" aria-hidden="true">
              {host}/
            </span>
            <input
              aria-label="Page address"
              list="prev-routes"
              value={address}
              spellCheck={false}
              autoComplete="off"
              onChange={(e) => setDraft(e.target.value.replace(/^#/, ""))}
              onBlur={() => setDraft(null)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  setDraft(null);
                  (e.target as HTMLInputElement).blur();
                }
              }}
            />
            <datalist id="prev-routes">
              {routes.map((r) => (
                <option key={r.path} value={`#${r.path}`}>
                  {r.title}
                </option>
              ))}
            </datalist>
          </form>
        ) : (
          <span className="prev-url" title={address}>
            {address}
          </span>
        )}
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
        {stage && (
          <div className="prev-tools">
            <div className="prev-devices" role="group" aria-label="Device width">
              {(["desktop", "tablet", "mobile"] as const).map((d) => (
                <button
                  key={d}
                  type="button"
                  className="prev-tool"
                  aria-pressed={device === d}
                  onClick={() => pickDevice(d)}
                  title={`${d[0].toUpperCase() + d.slice(1)} — ${DEVICE_WIDTH[d]}px`}
                  aria-label={`${d[0].toUpperCase() + d.slice(1)}, ${DEVICE_WIDTH[d]} pixels`}
                >
                  {d === "desktop" ? Icon.monitor : d === "tablet" ? Icon.tablet : Icon.phone}
                </button>
              ))}
            </div>
            <button
              type="button"
              className="prev-size mono"
              onClick={() => setFit((f) => !f)}
              title={fit ? "Fitted to the column. Click for actual size." : "Actual size. Click to fit the column."}
              aria-label={`Width ${width} pixels, ${fit && scale < 1 ? `scaled to ${Math.round(scale * 100)} percent` : "actual size"}. Toggle fit.`}
            >
              {width}
              <span className="prev-size-x" aria-hidden="true">
                ·
              </span>
              {fit && scale < 1 ? `${Math.round(scale * 100)}%` : "100%"}
            </button>
            <span className="prev-tools-rule" aria-hidden="true" />
            <button type="button" className="prev-tool" onClick={() => setReloads((n) => n + 1)} title="Reload the mockup" aria-label="Reload">
              {Icon.refresh}
            </button>
            <button type="button" className="prev-tool" onClick={openInTab} title="Open in a new tab" aria-label="Open in a new tab">
              {Icon.external}
            </button>
            <button
              type="button"
              className="prev-tool"
              onClick={toggleFullscreen}
              aria-pressed={full}
              title={full ? "Exit fullscreen (Esc)" : "Fullscreen"}
              aria-label={full ? "Exit fullscreen" : "Fullscreen"}
            >
              {full ? Icon.shrink : Icon.expand}
            </button>
          </div>
        )}
      </div>
      {stage ? (
        <div className="prev-stage" ref={stageRef}>
          <div
            className="prev-device"
            data-device={device}
            style={{ width: Math.round(width * scale), height: frameH ? Math.round(frameH * scale) : undefined }}
          >
            {iframe}
            <div
              className="prev-grip"
              role="slider"
              tabIndex={0}
              aria-label="Frame width"
              aria-orientation="horizontal"
              aria-valuemin={MIN_W}
              aria-valuemax={MAX_W}
              aria-valuenow={width}
              aria-valuetext={`${width} pixels`}
              title="Drag to resize — or use the arrow keys"
              onPointerDown={startResize}
              onKeyDown={resizeKey}
            />
          </div>
        </div>
      ) : (
        iframe
      )}
    </div>
  );
});

export default MockupFrame;
