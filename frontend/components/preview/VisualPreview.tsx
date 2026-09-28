"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, PatchOp, PreviewSection, PreviewState, ThemeTokens } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import MockupFrame, { type MockupFrameHandle } from "./MockupFrame";
import BuildProgress from "./BuildProgress";
import MockupReport from "./MockupReport";
import Inspector, { type Pending, type Selection } from "./Inspector";
import * as tw from "./tw";

/**
 * The mockup, and the two things a person does with it: use it, and change it.
 *
 * It is a small working site, so the default is to *use* it, the way anyone judging
 * a prototype would, at a real device width. Editing is a mode you switch into
 * (the Edit button, or S / ⌘I): hover shows what a click will pick, a click selects
 * any element — the navbar, one link, a heading, an icon — and the inspector beside
 * the canvas changes it.
 *
 * Direct edits show at once and queue as pending changes; Apply saves them as one
 * revision without asking a model. A change that needs one ("Ask the crew") goes to
 * the model scoped to the selected element, and comes back as a before/after to keep
 * or throw away. Undo and redo walk the revisions; the original build is never lost.
 *
 * A build is many model calls, so generating does not hold a request open: it
 * starts, and this polls the build's progress until the new mockup lands.
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

export default function VisualPreview({ id }: { id: string }) {
  const [state, setState] = useState<PreviewState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Failure | null>(null);
  const [notice, setNotice] = useState<string>("");
  const [mode, setMode] = useState<Mode>("use");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [changes, setChangesState] = useState<Change[]>([]);
  const [redoStack, setRedoState] = useState<Change[]>([]);
  // A model edit just landed: `before` is the revision it was made from.
  const [review, setReview] = useState<{ before: string; label: string; showing: "after" | "before"; html?: string } | null>(null);
  const frameRef = useRef<MockupFrameHandle>(null);
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

  const refresh = useCallback(async () => {
    const ticket = ++asked.current;
    try {
      const next = await api.getPreview(id);
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
  useEffect(() => {
    if (!building) return;
    const timer = setInterval(async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        await refresh();
      } finally {
        inFlight.current = false;
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [building, refresh]);

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
    const ok = await run("Save the changes", () => api.patchPreview(id, list.map(forward), what.join(", ").slice(0, 190)));
    if (ok) resetQueue();
  };

  const generate = () => {
    resetQueue();
    setReview(null);
    return run("Start the build", () => api.generatePreview(id));
  };
  const undo = () => {
    setReview(null);
    return run("Undo", () => api.undoPreview(id));
  };
  const redo = () => {
    setReview(null);
    return run("Redo", () => api.redoPreview(id));
  };

  const ask = async (instruction: string) => {
    if (!selection) return false;
    const before = state?.head_id ?? null;
    const label = selection.info.name;
    const ok = await run("Make that change", () => api.editPreviewElement(id, selection.info.oid, instruction));
    if (ok && before) setReview({ before, label, showing: "after" });
    return ok;
  };

  const restyle = (tokens: Partial<ThemeTokens>) => run("Restyle the site", () => api.themePreview(id, tokens));

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
      if (mod && k === "i" && stateRef.current?.html) {
        e.preventDefault();
        setMode((m) => (m === "edit" ? "use" : "edit"));
      } else if (mod && k === "z" && modeRef.current === "edit") {
        e.preventDefault();
        if (e.shiftKey) actions.current.redoPending();
        else actions.current.undoPending();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (mode === "use") {
      setSelection(null);
      frameRef.current?.select(null);
    }
  }, [mode]);

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

  const comparing = review?.showing === "before" && Boolean(review.html);
  const html = comparing ? review!.html! : state?.html ?? null;
  const job = state?.job ?? null;
  const failed = job && !job.running && job.error ? job.error : null;

  // Sections grouped by the page they are on, shared chrome first.
  const groups = useMemo(() => {
    if (!state) return [];
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
  }, [state]);

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
          <span className="notice-title">{error ? error.title : "The last build didn't finish"}</span>
          <span className="notice-text">{error ? error.text : failed}</span>
          {failed && !error && (
            <div className="notice-actions">
              <button className="btn btn-sm btn-primary" disabled={busy || building} onClick={generate}>
                {Icon.refresh} Build it again
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

  // Nothing yet, and nothing building — the tab teaches what it is for.
  if (!html && !building) {
    return (
      <div className="mk-stack">
        <div className="card empty">
          <h3>{state && !state.has_frontend ? "Prism hasn't built the front end yet" : "No mockup yet"}</h3>
          <p>
            {state && !state.has_frontend
              ? "The mockup is drawn when the Frontend phase finishes. You can build one now from the design so far."
              : "The Frontend phase builds this on its own. Something stopped it from landing — build it now and it will be here for the Ship review."}{" "}
            It&apos;s a clickable site: several pages, sample records, forms that validate and store, lists you can
            search and sort. Switch to Edit to select any element and restyle it.
          </p>
          <button className="btn btn-primary" disabled={busy} onClick={generate}>
            {busy && <span className="btn-spinner" aria-hidden="true" />}
            {busy ? "Starting…" : "Build mockup"}
            {!busy && Icon.sparkle}
          </button>
        </div>
        {errorNotice}
      </div>
    );
  }

  if (!html && job) {
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
  const legacy = !state!.routes.length;

  return (
    <div className="mk-stack">
      <div className="prev-toolbar">
        <h3 className="label">Mockup</h3>
        <span className="badge badge-mono" title="Every build, edit and restyle is kept">
          {revisions} rev{revisions === 1 ? "" : "s"}
        </span>
        <span className="rule" />
        <div className="switcher" role="group" aria-label="What a click in the mockup does">
          <button
            type="button"
            className="seg-btn seg-btn-icon"
            aria-pressed={mode === "use"}
            onClick={() => setMode("use")}
            title="Click through the prototype: links, forms, filters (S or ⌘I)"
          >
            {Icon.pointer} Use
          </button>
          <button
            type="button"
            className="seg-btn seg-btn-icon"
            aria-pressed={editing}
            onClick={() => setMode("edit")}
            title="Select any element to change it (S or ⌘I)"
          >
            {Icon.pen} Edit
          </button>
        </div>
        <button
          className="btn btn-sm"
          disabled={busy || building || pending || !state!.can_undo}
          onClick={undo}
          title={pending ? "Apply or discard your pending changes first" : state!.can_undo ? "Go back one version" : "This is the original build"}
        >
          {Icon.undo} Undo
        </button>
        <button
          className="btn btn-sm"
          disabled={busy || building || pending || !state!.can_redo}
          onClick={redo}
          title={pending ? "Apply or discard your pending changes first" : "Go forward one version"}
        >
          {Icon.redo} Redo
        </button>
        <button className="btn btn-sm" disabled={busy || building} onClick={generate} title="Draw the whole mockup again from the build">
          {busy && !building ? <span className="btn-spinner" aria-hidden="true" /> : Icon.refresh}
          Rebuild
        </button>
      </div>

      {building && job && (
        <>
          <BuildProgress job={job} />
          <p className="field-hint">The mockup below stays until the new one is ready.</p>
        </>
      )}
      {errorNotice}
      {state!.report && <MockupReport report={state!.report} />}

      {review && (
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
          <MockupFrame
            ref={frameRef}
            html={html!}
            routes={state!.routes}
            selectable
            stage
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
              (comparing
                ? "Showing the version before the crew's change."
                : editing
                  ? "Hover to see what a click picks. ↑ or Esc selects the parent, ↵ the first child, right-click lists every layer under the pointer."
                  : legacy
                    ? "This mockup is a single static page, drawn before mockups were built as sites. Rebuild it for pages, sample data and forms that work."
                    : "Click through it like a user: the pages, forms and filters work. Switch to Edit (S) to change anything.")}
          </p>
        </div>
        {editing && (
          <Inspector
            selection={selection}
            classesOf={classesOf}
            theme={state!.theme}
            sections={groups}
            pending={changes}
            canUndoPending={changes.length > 0}
            canRedoPending={redoStack.length > 0}
            busy={busy || building}
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
