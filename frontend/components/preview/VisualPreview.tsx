"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, PatchOp, PreviewLocate, PreviewSection, PreviewState, PreviewTarget, ThemeTokens } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import PreviewFrame, { type PreviewFrameHandle } from "./PreviewFrame";
import BuildProgress from "./BuildProgress";
import MockupReport from "./MockupReport";
import Inspector, { type Pending, type Selection } from "./Inspector";
import { AppStarting, ChangeStrip, SourceLine } from "./AppStatus";
import * as tw from "./tw";

/**
 * The preview, and the two things a person does with it: use it, and change it.
 *
 * Once the crew has written a frontend that builds, what is on screen is **the app
 * itself** (#78): the generated code, built and running in a sandbox, at a real
 * device width. Before that — or when the app can't run here — it is **the sketch**
 * drawn from the plan, and the line above the frame says which, and why.
 *
 * Editing is a mode you switch into (the Edit button, or S / ⌘I): hover shows what a
 * click will pick, a click selects any element — the navbar, one link, a heading, an
 * icon — and the inspector beside the canvas changes it.
 *
 * On the app, a change is a change to the code. Direct edits show at once and queue as
 * pending changes; Apply writes them into the JSX that renders each element — where
 * the code states it plainly — as a new Frontend attempt, checked and built like any
 * other, and the app restarts with it. "Ask the crew" sends the element's file to the
 * Frontend Engineer with the change. Undo brings back the code before the last change.
 *
 * On the sketch, the same edits change the sketch's HTML, a model change comes back
 * as a before/after to keep or throw away, and undo and redo walk its revisions.
 *
 * Starting the app and drawing the sketch both take minutes, so neither holds a request
 * open: they start, and this polls until they land.
 */

type Mode = "use" | "edit";

const POLL_MS = 2500;

/** One queued direct edit, with what it replaced so it can be undone in place. */
type Change = Pending &
  (
    | { kind: "classes"; before: string; after: string }
    | { kind: "text"; before: string; after: string }
    | { kind: "attr"; name: "href" | "alt"; before: string | null; after: string }
  );

function forward(c: Change): PatchOp {
  if (c.kind === "classes") return { oid: c.oid, kind: "classes", ...tw.diff(c.before, c.after) };
  if (c.kind === "text") return { oid: c.oid, kind: "text", text: c.after };
  return { oid: c.oid, kind: "attr", name: c.name, value: c.after };
}
function backward(c: Change): PatchOp {
  if (c.kind === "classes") return { oid: c.oid, kind: "classes", ...tw.diff(c.after, c.before) };
  if (c.kind === "text") return { oid: c.oid, kind: "text", text: c.before };
  return { oid: c.oid, kind: "attr", name: c.name, value: c.before };
}
function isNoop(c: Change): boolean {
  if (c.kind === "classes") {
    const d = tw.diff(c.before, c.after);
    return !d.add.length && !d.remove.length;
  }
  return c.before === c.after;
}

type Failure = { title: string; text: string };

/** Copy that says what went wrong and what to do, by what the server answered. */
function describe(e: unknown, doing: string): Failure {
  const text = e instanceof Error ? e.message : String(e);
  const status = e instanceof ApiError ? e.status : 0;
  if (status === 409) return { title: "Not right now", text };
  if (status === 422) return { title: "That change wasn't applied", text };
  if (status === 502) return { title: "The model didn't answer", text };
  if (!status && /abort|timed? ?out/i.test(text)) return { title: `${doing} took too long`, text: "The server didn't answer in time. Try again." };
  return { title: `Couldn't ${doing[0].toLowerCase()}${doing.slice(1)}`, text };
}

/** What the app is doing that the tab should follow: starting, or being changed. */
function appMoving(state: PreviewState | null): boolean {
  const app = state?.app;
  if (!app) return false;
  return (
    ["starting", "idle", "waiting"].includes(app.status) ||
    Boolean(app.editing?.active) ||
    (app.stale && app.status !== "failed")
  );
}

export default function VisualPreview({ id, onOpenBuild }: { id: string; onOpenBuild?: () => void }) {
  const [state, setState] = useState<PreviewState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Failure | null>(null);
  const [notice, setNotice] = useState<string>("");
  const [mode, setMode] = useState<Mode>("use");
  // The Preview tab over the whole browser window: toolbar, canvas and inspector.
  const [expanded, setExpanded] = useState(false);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [changes, setChangesState] = useState<Change[]>([]);
  const [redoStack, setRedoState] = useState<Change[]>([]);
  // A model edit just landed: `before` is the revision it was made from.
  const [review, setReview] = useState<{ before: string; label: string; showing: "after" | "before"; html?: string } | null>(null);
  const frameRef = useRef<PreviewFrameHandle>(null);
  // Where the selected app element is in the code, and what can change it there.
  const [where, setWhere] = useState<PreviewLocate | null>(null);
  // A refused change, once dismissed.
  const [dismissed, setDismissed] = useState<string | null>(null);
  const nextId = useRef(1);
  const live = useRef(new Map<string, string>()); // oid → classes, pending changes included

  // The queue lives in refs as well as state: frame messages and key presses arrive
  // between renders, and each must see the change before it.
  const changesRef = useRef<Change[]>([]);
  const redoRef = useRef<Change[]>([]);
  const setChanges = useCallback((next: Change[]) => {
    changesRef.current = next;
    setChangesState(next);
  }, []);
  const setRedo = useCallback((next: Change[]) => {
    redoRef.current = next;
    setRedoState(next);
  }, []);

  // Responses are applied in the order they were asked for. The backend is busy while
  // it builds, so a slow poll can come back after a faster, newer one — and applying
  // it would put the old mockup back and turn "building" on again.
  const asked = useRef(0);
  const applied = useRef(0);
  const inFlight = useRef(false);

  const refresh = useCallback(async (touch = false) => {
    const ticket = ++asked.current;
    try {
      const next = await api.getPreview(id, touch);
      if (ticket < applied.current) return;
      applied.current = ticket;
      setState(next);
      setError(null);
    } catch (e) {
      if (ticket < applied.current) return;
      setError(describe(e, "Load the mockup"));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // While a build runs, follow it. An interval rather than a timeout re-armed by each
  // new state: a failed request leaves the state as it was, and a poll that only
  // re-arms on change would stop there for good.
  const building = Boolean(state?.job?.running);
  const moving = appMoving(state);
  useEffect(() => {
    if (!building && !moving) return;
    const timer = setInterval(async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        await refresh(true);
      } finally {
        inFlight.current = false;
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [building, moving, refresh]);

  // ── the running app (#78) ────────────────────────────────────────────────
  const app = state?.app ?? null;
  const onApp = state?.source === "app";
  const appUrl = onApp ? app?.url ?? null : null;
  const to: PreviewTarget | undefined = onApp ? { target: "app", built_from: app?.built_from } : undefined;

  // Start the app whenever the current frontend has none running or starting — for a
  // new attempt at once, and again when it went idle (stopped after the idle limit, or
  // a start the sandbox couldn't finish). Not more than every 15 s: a start that keeps
  // failing for the sandbox's sake says so rather than spinning.
  const startedFor = useRef<{ from: string; at: number } | null>(null);
  useEffect(() => {
    if (!app || app.status !== "idle" || !app.current_from) return;
    const last = startedFor.current;
    if (last && last.from === app.current_from && Date.now() - last.at < 15_000) return;
    startedFor.current = { from: app.current_from, at: Date.now() };
    api
      .startPreviewApp(id)
      .then((next) => setState(next))
      .catch((e) => setError(describe(e, "Start the app")));
  }, [app, id]);

  // While the app is on screen, say so now and then, so it isn't stopped as idle.
  useEffect(() => {
    if (!appUrl) return;
    const beat = () => {
      if (document.visibilityState === "visible") refresh(true);
    };
    const timer = setInterval(beat, 60_000);
    document.addEventListener("visibilitychange", beat);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", beat);
    };
  }, [appUrl, refresh]);

  // ── the queue ────────────────────────────────────────────────────────────
  const classesOf = useCallback((oid: string, fallback: string) => live.current.get(oid) ?? fallback, []);

  const push = useCallback(
    (change: Change) => {
      setRedo([]);
      const list = changesRef.current;
      const last = list[list.length - 1];
      // Steps on the same control fold into one change: ten clicks of "+" are one edit.
      if (last && last.oid === change.oid && last.key === change.key && last.kind === change.kind) {
        const merged = { ...change, id: last.id, before: last.before } as Change;
        setChanges(isNoop(merged) ? list.slice(0, -1) : [...list.slice(0, -1), merged]);
        return;
      }
      setChanges([...list, change]);
    },
    [setChanges, setRedo],
  );

  const onClasses = useCallback(
    (key: string, label: string, compute: (classes: string) => string[]) => {
      if (!selection) return;
      const targets = [
        { oid: selection.info.oid, classes: selection.info.classes, templated: selection.info.templated, name: selection.info.name },
        ...selection.others,
      ].filter((t) => !t.templated);
      const ops: PatchOp[] = [];
      for (const t of targets) {
        const before = live.current.get(t.oid) ?? t.classes;
        const after = compute(before).join(" ");
        const d = tw.diff(before, after);
        if (!d.add.length && !d.remove.length) continue;
        live.current.set(t.oid, after);
        ops.push({ oid: t.oid, kind: "classes", ...d });
        push({ id: nextId.current++, oid: t.oid, key, label: `${t.name} · ${label}`, kind: "classes", before, after });
      }
      if (ops.length) frameRef.current?.apply(ops);
    },
    [selection, push],
  );

  const onText = useCallback(
    (text: string) => {
      if (!selection || selection.info.text === null) return;
      const { oid, name } = selection.info;
      frameRef.current?.apply([{ oid, kind: "text", text }]);
      push({ id: nextId.current++, oid, key: "text", label: `${name} · Text`, kind: "text", before: selection.info.text, after: text });
    },
    [selection, push],
  );

  const onAttr = useCallback(
    (name: "href" | "alt", value: string) => {
      if (!selection) return;
      const { oid } = selection.info;
      frameRef.current?.apply([{ oid, kind: "attr", name, value }]);
      push({
        id: nextId.current++,
        oid,
        key: name,
        label: `${selection.info.name} · ${name === "href" ? "Link" : "Alt text"}`,
        kind: "attr",
        name,
        before: selection.info.attrs[name],
        after: value,
      });
    },
    [selection, push],
  );

  const resetQueue = useCallback(() => {
    setChanges([]);
    setRedo([]);
    live.current.clear();
  }, [setChanges, setRedo]);

  const track = (c: Change, on: boolean) => {
    if (c.kind === "classes") live.current.set(c.oid, on ? c.after : c.before);
  };

  const undoPending = useCallback(() => {
    const list = changesRef.current;
    const last = list[list.length - 1];
    if (!last) return;
    frameRef.current?.apply([backward(last)]);
    track(last, false);
    setRedo([...redoRef.current, last]);
    setChanges(list.slice(0, -1));
  }, [setChanges, setRedo]);

  const redoPending = useCallback(() => {
    const stack = redoRef.current;
    const next = stack[stack.length - 1];
    if (!next) return;
    frameRef.current?.apply([forward(next)]);
    track(next, true);
    setChanges([...changesRef.current, next]);
    setRedo(stack.slice(0, -1));
  }, [setChanges, setRedo]);

  // Discarding one change from the middle: take everything back, then put the rest
  // on again in order, so each keeps meaning what it did.
  const discard = useCallback(
    (cid: number) => {
      const list = changesRef.current;
      const keep = list.filter((c) => c.id !== cid);
      frameRef.current?.apply([...[...list].reverse().map(backward), ...keep.map(forward)]);
      live.current.clear();
      for (const c of keep) track(c, true);
      setChanges(keep);
      setRedo([]);
    },
    [setChanges, setRedo],
  );

  const discardAll = useCallback(() => {
    frameRef.current?.apply([...changesRef.current].reverse().map(backward));
    resetQueue();
  }, [resetQueue]);

  // ── talking to the server ────────────────────────────────────────────────
  async function run(doing: string, fn: () => Promise<PreviewState>): Promise<boolean> {
    setBusy(true);
    setError(null);
    try {
      setState(await fn());
      return true;
    } catch (e) {
      setError(describe(e, doing));
      return false;
    } finally {
      setBusy(false);
    }
  }

  const applyAll = async () => {
    const list = changesRef.current;
    if (!list.length) return;
    const what = Array.from(new Set(list.map((c) => c.label.split(" · ").pop() ?? c.label)));
    const ok = await run(onApp ? "Write the changes into the code" : "Save the changes", () =>
      api.patchPreview(id, list.map(forward), what.join(", ").slice(0, 190), to),
    );
    // On the app the edits stay on the page until it restarts with them in the code.
    if (ok) resetQueue();
  };

  const generate = () => {
    resetQueue();
    setReview(null);
    return run("Start the build", () => api.generatePreview(id));
  };
  const undo = () => {
    setReview(null);
    return run("Undo", () => api.undoPreview(id, to));
  };
  const redo = () => {
    setReview(null);
    return run("Redo", () => api.redoPreview(id, to));
  };

  const ask = async (instruction: string) => {
    if (!selection) return false;
    if (onApp) {
      // The crew changes the file; the app restarts with it. Undo brings the old back.
      return run("Send the change to the crew", () => api.editPreviewElement(id, selection.info.oid, instruction, to));
    }
    const before = state?.head_id ?? null;
    const label = selection.info.name;
    const ok = await run("Make that change", () => api.editPreviewElement(id, selection.info.oid, instruction));
    if (ok && before) setReview({ before, label, showing: "after" });
    return ok;
  };

  const restyle = (tokens: Partial<ThemeTokens>) => run("Restyle the site", () => api.themePreview(id, tokens, to));
  const retryApp = () => run("Start the app", () => api.startPreviewApp(id, true));

  async function compare(which: "before" | "after") {
    if (!review) return;
    if (which === "before" && !review.html) {
      try {
        const rev = await api.getPreviewRevision(id, review.before);
        setReview({ ...review, html: rev.html, showing: "before" });
      } catch (e) {
        setError(describe(e, "Load the earlier version"));
      }
      return;
    }
    setReview({ ...review, showing: which });
  }

  // ── messages from the frame ──────────────────────────────────────────────
  const selectionRef = useRef(selection);
  selectionRef.current = selection;
  const stateRef = useRef(state);
  stateRef.current = state;
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const actions = useRef({ undoPending, redoPending, undo, push });
  actions.current = { undoPending, redoPending, undo, push };

  useEffect(() => {
    function onMsg(e: MessageEvent) {
      if (!frameRef.current?.owns(e.source)) return;
      const d = e.data;
      if (!d || !d.__preview) return;
      if (d.type === "select" && d.info) {
        setSelection({ info: d.info, others: d.others ?? [] });
        setNotice("");
      } else if (d.type === "deselect") {
        setSelection(null);
      } else if (d.type === "toggleMode") {
        setMode((m) => (m === "edit" ? "use" : "edit"));
      } else if (d.type === "notice" && typeof d.text === "string") {
        setNotice(d.text);
      } else if (d.type === "textEdited" && typeof d.oid === "string") {
        const sel = selectionRef.current;
        const name = sel && sel.info.oid === d.oid ? sel.info.name : "Text";
        actions.current.push({ id: nextId.current++, oid: d.oid, key: "text", label: `${name} · Text`, kind: "text", before: String(d.prev ?? ""), after: String(d.text ?? "") });
      } else if (d.type === "undo") {
        if (changesRef.current.length) actions.current.undoPending();
        else if (stateRef.current?.can_undo) actions.current.undo();
      } else if (d.type === "redo") {
        actions.current.redoPending();
      }
    }
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, []);

  // ⌘I and ⌘Z work with the focus outside the frame too.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      const mod = e.metaKey || e.ctrlKey;
      const k = e.key.toLowerCase();
      const canPick = Boolean(stateRef.current?.html || stateRef.current?.app?.url);
      if (mod && k === "i" && canPick) {
        e.preventDefault();
        setMode((m) => (m === "edit" ? "use" : "edit"));
      } else if (mod && k === "z" && modeRef.current === "edit") {
        e.preventDefault();
        if (e.shiftKey) actions.current.redoPending();
        else actions.current.undoPending();
      } else if (!mod && !e.altKey && modeRef.current === "edit" && selectionRef.current && ["ArrowUp", "ArrowDown", "Enter", "Escape", "F2"].includes(e.key)) {
        // The selection keys work wherever the focus is — after clicking a
        // breadcrumb or a toolbar button, it is out here, not in the frame.
        if (e.key === "Enter" && t && /^(BUTTON|A)$/.test(t.tagName)) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        frameRef.current?.key(e.key, e.shiftKey);
      } else if (!mod && !e.altKey && k === "s" && canPick) {
        e.preventDefault();
        setMode((m) => (m === "edit" ? "use" : "edit"));
      }
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  useEffect(() => {
    if (mode === "use") {
      setSelection(null);
      frameRef.current?.select(null);
    }
  }, [mode]);

  // The app and the sketch are different pages: a selection or a queued edit on one
  // means nothing on the other.
  const shownSource = state?.source ?? null;
  useEffect(() => {
    setSelection(null);
    resetQueue();
    setReview(null);
  }, [shownSource, resetQueue]);

  // On the app, ask the code where the selected element is drawn.
  const selectedOid = onApp ? selection?.info.oid ?? null : null;
  useEffect(() => {
    setWhere(null);
    if (!selectedOid) return;
    let live = true;
    api
      .locatePreview(id, selectedOid)
      .then((w) => live && setWhere(w))
      .catch(() => live && setWhere(null));
    return () => {
      live = false;
    };
  }, [id, selectedOid]);

  // Full window: Esc leaves it, and the page underneath stops scrolling.
  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key === "Escape" && !(t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) setExpanded(false);
    };
    const before = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = before;
      window.removeEventListener("keydown", onKey);
    };
  }, [expanded]);

  // Leaving with unsaved edits loses them: say so.
  useEffect(() => {
    if (!changes.length) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [changes.length]);

  const comparing = !onApp && review?.showing === "before" && Boolean(review.html);
  const html = onApp ? null : comparing ? review!.html! : state?.html ?? null;
  const job = state?.job ?? null;
  const failed = job && !job.running && job.error ? job.error : null;
  const appStarting = onApp && !appUrl;
  const restarting = Boolean(onApp && app && (app.stale || (app.status !== "running" && Boolean(app.url))));
  const edit = app?.editing ?? null;
  const editShown = edit && !(edit.status === "refused" && dismissed === edit.at) ? edit : null;
  const changing = Boolean(onApp && app?.editing?.active);
  const appFailed = app?.status === "failed";

  // Sections grouped by the page they are on, shared chrome first.
  const groups = useMemo(() => {
    if (!state || onApp) return [];
    const byRoute = new Map<string, PreviewSection[]>();
    for (const s of state.sections) {
      const key = s.route ?? "";
      byRoute.set(key, [...(byRoute.get(key) ?? []), s]);
    }
    const out: { key: string; title: string; sections: PreviewSection[] }[] = [];
    if (!state.routes.length) {
      return state.sections.length ? [{ key: "all", title: "Sections", sections: state.sections }] : [];
    }
    const shared = byRoute.get("");
    if (shared?.length) out.push({ key: "shared", title: "Every page", sections: shared });
    for (const r of state.routes) {
      const list = byRoute.get(r.path);
      if (list?.length) out.push({ key: r.path, title: r.title, sections: list });
    }
    return out;
  }, [state, onApp]);

  if (loading && !state) {
    return (
      <div className="card">
        <SkeletonLines lines={3} />
      </div>
    );
  }

  const errorNotice =
    error || failed ? (
      <div className="notice notice-bad" role="alert">
        {Icon.alert}
        <div className="notice-body">
          <span className="notice-title">{error ? error.title : "The last sketch didn't finish"}</span>
          <span className="notice-text">{error ? error.text : failed}</span>
          {failed && !error && (
            <div className="notice-actions">
              <button className="btn btn-sm btn-primary" disabled={busy || building} onClick={generate}>
                {Icon.refresh} Draw it again
              </button>
            </div>
          )}
        </div>
        {error && (
          <button type="button" className="pv-x pv-dismiss" onClick={() => setError(null)} aria-label="Dismiss">
            {Icon.close}
          </button>
        )}
      </div>
    ) : null;

  // Why the app isn't on screen, when the frontend exists and it should be.
  const appNotice =
    !onApp && app && (app.status === "failed" || app.status === "unavailable") ? (
      <div className={"notice " + (appFailed ? "notice-bad" : "notice-warn")} role="note">
        {appFailed ? Icon.alert : Icon.info}
        <div className="notice-body">
          <span className="notice-title">{appFailed ? "The app didn't build, so this is the sketch" : "The app can't run here, so this is the sketch"}</span>
          <span className="notice-text">{app.reason}</span>
          {appFailed && app.problems.length > 0 && (
            <ul className="pv-problems">
              {app.problems.slice(0, 4).map((pr, i) => (
                <li key={i}>
                  {pr.path && <code>{pr.path.replace(/^frontend\//, "")}{pr.line ? `:${pr.line}` : ""}</code>} {pr.message}
                </li>
              ))}
            </ul>
          )}
          {appFailed && (
            <div className="notice-actions">
              {onOpenBuild && (
                <button className="btn btn-sm" onClick={onOpenBuild}>
                  {Icon.list} See Build
                </button>
              )}
              <button className="btn btn-sm btn-ghost" disabled={busy} onClick={retryApp}>
                {Icon.refresh} Try the app again
              </button>
            </div>
          )}
        </div>
      </div>
    ) : null;

  // Nothing yet, and nothing building — the tab teaches what it is for.
  if (!onApp && !html && !building) {
    const noFrontend = !state?.has_frontend;
    return (
      <div className="mk-stack">
        <div className="card empty">
          <h3>
            {noFrontend
              ? "Prism hasn't built the front end yet"
              : appFailed
                ? "The app didn't build"
                : app?.status === "unavailable"
                  ? "The app can't run here"
                  : "No preview yet"}
          </h3>
          <p>
            {noFrontend
              ? "Once the crew writes the frontend, this tab runs the app itself — the code you'll download and deploy. Until then you can see a sketch of it, drawn from the plan."
              : app?.reason
                ? `${app.reason} You can see a sketch of it instead, drawn from the plan.`
                : "You can see a sketch of it, drawn from the plan."}{" "}
            A sketch is a clickable site with pages, sample records and working forms — but it isn&apos;t the code.
          </p>
          <div className="notice-actions">
            <button className="btn btn-primary" disabled={busy} onClick={generate}>
              {busy && <span className="btn-spinner" aria-hidden="true" />}
              {busy ? "Starting…" : "Draw a sketch"}
              {!busy && Icon.sparkle}
            </button>
            {appFailed && (
              <button className="btn" disabled={busy} onClick={retryApp}>
                {Icon.refresh} Try the app again
              </button>
            )}
            {appFailed && onOpenBuild && (
              <button className="btn btn-ghost" onClick={onOpenBuild}>
                See Build
              </button>
            )}
          </div>
        </div>
        {errorNotice}
      </div>
    );
  }

  if (!onApp && !html && job) {
    return (
      <div className="mk-stack">
        <BuildProgress job={job} />
        {errorNotice}
      </div>
    );
  }

  const revisions = state!.revisions.length;
  const editing = mode === "edit";
  const pending = changes.length > 0;
  const legacy = !onApp && !state!.routes.length;
  const canUndo = onApp ? Boolean(app?.can_undo) : state!.can_undo;
  const canRedo = onApp ? Boolean(app?.can_redo) : state!.can_redo;
  const locked = busy || building || changing;
  const appBlock = onApp && app && !app.can_edit ? app.edit_block : null;

  return (
    <div className={"mk-stack" + (expanded ? " is-window" : "")} role={expanded ? "dialog" : undefined} aria-modal={expanded || undefined} aria-label={expanded ? (onApp ? "The app, full window" : "Sketch, full window") : undefined}>
      <div className="prev-toolbar">
        <h3 className="label">{onApp ? "App" : "Sketch"}</h3>
        <SourceLine state={state!} onOpenBuild={onOpenBuild} />
        {!onApp && (
          <span className="badge badge-mono" title="Every build, edit and restyle is kept">
            {revisions} rev{revisions === 1 ? "" : "s"}
          </span>
        )}
        <span className="rule" />
        <div className="switcher" role="group" aria-label="What a click in the preview does">
          <button
            type="button"
            className="seg-btn seg-btn-icon"
            aria-pressed={mode === "use"}
            onClick={() => setMode("use")}
            title={onApp ? "Use the app: links, forms, everything it does (S or ⌘I)" : "Click through the prototype: links, forms, filters (S or ⌘I)"}
          >
            {Icon.pointer} Use
          </button>
          <button
            type="button"
            className="seg-btn seg-btn-icon"
            aria-pressed={editing}
            disabled={appStarting}
            onClick={() => setMode("edit")}
            title={onApp ? "Select any element to change its code (S or ⌘I)" : "Select any element to change it (S or ⌘I)"}
          >
            {Icon.pen} Edit
          </button>
        </div>
        <button
          className="btn btn-sm"
          disabled={locked || pending || !canUndo}
          onClick={undo}
          title={
            pending
              ? "Apply or discard your pending changes first"
              : canUndo
                ? onApp
                  ? "Bring back the code from before your last change"
                  : "Go back one version"
                : onApp
                  ? "Nothing to undo — this is the code the crew wrote"
                  : "This is the original build"
          }
        >
          {Icon.undo} Undo
        </button>
        <button
          className="btn btn-sm"
          disabled={locked || pending || !canRedo}
          onClick={redo}
          title={pending ? "Apply or discard your pending changes first" : onApp ? "Put your undone change back" : "Go forward one version"}
        >
          {Icon.redo} Redo
        </button>
        {!onApp && (
          <button className="btn btn-sm" disabled={busy || building} onClick={generate} title="Draw the whole sketch again from the plan">
            {busy && !building ? <span className="btn-spinner" aria-hidden="true" /> : Icon.refresh}
            Redraw
          </button>
        )}
        {expanded && (
          <button className="btn btn-sm" onClick={() => setExpanded(false)} title="Back to the page (Esc)">
            {Icon.shrink} Exit full window
          </button>
        )}
      </div>

      {!onApp && building && job && (
        <>
          <BuildProgress job={job} />
          <p className="field-hint">The sketch below stays until the new one is ready.</p>
        </>
      )}
      {errorNotice}
      {appNotice}
      {!onApp && state!.sketch_stale && !expanded && (
        <p className="field-hint pv-stale">
          {Icon.info} This sketch was drawn from an older version of the frontend. Redraw it to match the current plan.
        </p>
      )}
      {!onApp && state!.report && !expanded && <MockupReport report={state!.report} />}
      {onApp && (
        <ChangeStrip
          edit={editShown}
          restarting={restarting && !changing && Boolean(edit && edit.status === "landed")}
          onDismiss={() => setDismissed(edit?.at ?? null)}
        />
      )}

      {review && !onApp && (
        <div className="pv-review" role="status">
          {Icon.sparkle}
          <span className="pv-review-text">
            The crew changed <b>{review.label}</b>. Compare, then keep it or throw it away.
          </span>
          <div className="pv-seg" role="group" aria-label="Compare">
            <button type="button" className="pv-seg-btn" aria-pressed={review.showing === "before"} onClick={() => compare("before")}>
              Before
            </button>
            <button type="button" className="pv-seg-btn" aria-pressed={review.showing === "after"} onClick={() => compare("after")}>
              After
            </button>
          </div>
          <span className="rule" />
          <button type="button" className="btn btn-sm btn-ghost" disabled={busy} onClick={undo}>
            Throw it away
          </button>
          <button type="button" className="btn btn-sm btn-primary" onClick={() => setReview(null)}>
            {Icon.check} Keep
          </button>
        </div>
      )}

      <div className={"pv-work" + (editing ? " is-editing" : "")}>
        <div className="pv-canvas">
          <PreviewFrame
            ref={frameRef}
            html={html}
            src={appUrl}
            placeholder={app && appStarting ? <AppStarting app={app} restarting={Boolean(edit && edit.status === "landed")} /> : null}
            routes={onApp ? app?.routes ?? [] : state!.routes}
            selectable
            stage
            expanded={expanded}
            onExpand={() => setExpanded((v) => !v)}
            mode={comparing ? "use" : mode}
            onLoad={() => {
              // A new revision or a reload: the frame restores the page; put back the
              // edits not yet applied, and what was selected.
              const pendingOps = changesRef.current.map(forward);
              if (pendingOps.length) frameRef.current?.apply(pendingOps);
              const sel = selectionRef.current;
              if (modeRef.current === "edit" && sel) frameRef.current?.select(sel.info.oid, false);
            }}
          />
          <p className="field-hint pv-hint-line" aria-live="polite">
            {notice ||
              (onApp
                ? appStarting
                  ? "The app opens here once it has built."
                  : editing
                    ? appBlock
                      ? `${appBlock} You can still select elements to see where they come from.`
                      : "Changes here are made to the code. Hover to preview a selection; ↑ or Esc selects the parent."
                    : app?.backend?.status === "down" || app?.backend?.why
                      ? `The app runs here as built. ${app?.backend?.why ?? ""}`.trim()
                      : "This is the app as built — the code you download and deploy. Switch to Edit (S) to change it."
                : comparing
                  ? "Showing the version before the crew's change."
                  : editing
                    ? "Hover to preview a selection. ↑ or Esc selects the parent, ↵ the first child, right-click lists every layer."
                    : legacy
                      ? "This is an older single-page sketch. Redraw it for pages, sample data and working forms."
                      : "A sketch drawn from the plan: pages, forms and filters work, but it isn't the code. Switch to Edit (S) to change it.")}
          </p>
        </div>
        {editing && (
          <Inspector
            selection={selection}
            classesOf={classesOf}
            theme={onApp ? app?.theme ?? null : state!.theme}
            sections={groups}
            pending={changes}
            canUndoPending={changes.length > 0}
            canRedoPending={redoStack.length > 0}
            busy={locked}
            scope={onApp ? "app" : "sketch"}
            where={where}
            blocked={appBlock}
            onClasses={onClasses}
            onText={onText}
            onAttr={onAttr}
            onSelect={(oid) => frameRef.current?.select(oid, true)}
            onSection={(sid) => frameRef.current?.highlight(sid)}
            onTypeOnPage={() => frameRef.current?.editText()}
            onAsk={ask}
            onUndoPending={undoPending}
            onRedoPending={redoPending}
            onDiscard={discard}
            onDiscardAll={discardAll}
            onApply={applyAll}
            onTheme={restyle}
          />
        )}
      </div>
    </div>
  );
}
