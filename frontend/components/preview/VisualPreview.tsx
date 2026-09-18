"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, PreviewSection, PreviewState } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";
import MockupFrame, { type MockupFrameHandle } from "./MockupFrame";
import BuildProgress from "./BuildProgress";
import MockupReport from "./MockupReport";

/**
 * The mockup, and the two things a person does with it: use it, and change it.
 *
 * It is a small working site now — pages, sample records, forms that store, lists
 * that filter — so the default is to *use* it, the way anyone judging a prototype
 * would. Editing is a mode you switch into: clicks then select a section instead of
 * following it, and the change request goes to that one section.
 *
 * A build is many model calls, so generating no longer holds a request open: it
 * starts, and this polls the build's progress until the new mockup lands.
 */

type Mode = "use" | "edit";

const POLL_MS = 2500;

export default function VisualPreview({ id }: { id: string }) {
  const [state, setState] = useState<PreviewState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<PreviewSection | null>(null);
  const [instruction, setInstruction] = useState("");
  const [mode, setMode] = useState<Mode>("use");
  const frameRef = useRef<MockupFrameHandle>(null);

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
      // A blip that the next poll recovered from is not an error worth keeping on screen.
      setError("");
    } catch (e: any) {
      // The same rule for a failure: an older request failing after a newer one
      // landed says nothing about the state now on screen.
      if (ticket < applied.current) return;
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // While a build runs, follow it. Silent refreshes: no skeleton over a mockup that
  // is still perfectly readable while its replacement is drawn. An interval rather
  // than a timeout re-armed by each new state: a failed request leaves the state as
  // it was, and a poll that only re-arms on change would stop there for good —
  // leaving the tab on "building" with Rebuild disabled until someone reloads.
  const building = Boolean(state?.job?.running);
  useEffect(() => {
    if (!building) return;
    const timer = setInterval(async () => {
      if (inFlight.current) return; // a tick while the last poll is out is a tick skipped
      inFlight.current = true;
      try {
        await refresh();
      } finally {
        inFlight.current = false;
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [building, refresh]);

  // Selection clicks coming up from the sandboxed iframe (edit mode only).
  useEffect(() => {
    function onMsg(e: MessageEvent) {
      if (!frameRef.current?.owns(e.source)) return;
      const d = e.data;
      if (d && d.__preview && d.type === "select") setSelected({ id: d.id, label: d.label });
    }
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, []);

  useEffect(() => {
    frameRef.current?.highlight(mode === "edit" ? selected?.id ?? null : null);
  }, [selected, mode]);

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

  function pick(section: PreviewSection) {
    setMode("edit");
    setSelected(section);
  }

  function switchMode(next: Mode) {
    setMode(next);
    if (next === "use") setSelected(null);
  }

  const html = state?.html ?? null;
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
          <span className="notice-title">{failed ? "The last build didn't finish" : "That didn't work"}</span>
          <span className="notice-text">{error || failed}</span>
          {failed && !error && (
            <div className="notice-actions">
              <button className="btn btn-sm btn-primary" disabled={busy || building} onClick={generate}>
                {Icon.refresh} Build it again
              </button>
            </div>
          )}
        </div>
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
            search and sort. Switch to Edit to change any section in plain language.
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

  return (
    <div className="mk-stack">
      <div className="prev-toolbar">
        <h3 className="label">Mockup</h3>
        <span className="badge badge-mono">
          {revisions} rev{revisions === 1 ? "" : "s"}
        </span>
        <span className="rule" />
        <div className="switcher" role="group" aria-label="What a click in the mockup does">
          <button
            type="button"
            className="seg-btn seg-btn-icon"
            aria-pressed={mode === "use"}
            onClick={() => switchMode("use")}
            title="Click through the prototype: links, forms, filters"
          >
            {Icon.pointer} Use
          </button>
          <button
            type="button"
            className="seg-btn seg-btn-icon"
            aria-pressed={mode === "edit"}
            disabled={!state!.sections.length}
            onClick={() => switchMode("edit")}
            title="Click a section to describe a change to it"
          >
            {Icon.pen} Edit
          </button>
        </div>
        <button className="btn btn-sm" disabled={busy || building || revisions === 0} onClick={undo}>
          {Icon.undo} Undo
        </button>
        <button className="btn btn-sm" disabled={busy || building} onClick={generate}>
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

      <MockupFrame
        key={state!.revisions[0]?.id || "iframe"}
        ref={frameRef}
        html={html!}
        routes={state!.routes}
        selectable
        mode={mode}
        onLoad={() => frameRef.current?.highlight(mode === "edit" ? selected?.id ?? null : null)}
      />

      {mode === "edit" && selected ? (
        <div className="card mk-edit">
          <div className="sec-head">
            <h3 className="label" style={{ color: "var(--accent)" }}>
              Editing
            </h3>
            <span className="mk-edit-name">{selected.label}</span>
            <span className="rule" />
          </div>
          <div className="mk-edit-row">
            <div className="field mk-edit-field">
              <label htmlFor="prev-instruction">What should change?</label>
              <input
                id="prev-instruction"
                className="input"
                placeholder="e.g. make the headline bigger and add a Get started button"
                value={instruction}
                disabled={busy}
                onChange={(e) => setInstruction(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") applyEdit();
                  if (e.key === "Escape") setSelected(null);
                }}
              />
            </div>
            <button className="btn btn-accent" disabled={busy || building || !instruction.trim()} onClick={applyEdit}>
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
          {busy && (
            <p className="field-hint" aria-live="polite">
              Rewriting this one section — a second call if the first loses what makes it work.
            </p>
          )}
        </div>
      ) : (
        <p className="field-hint">
          {mode === "edit"
            ? "Click any section in the mockup — or pick one below — then describe the change you want."
            : state!.routes.length
              ? "Click through it like a user: the pages, forms and filters work. Switch to Edit to change a section."
              : "This mockup is a single static page, drawn before mockups were built as sites. Rebuild it for pages, sample data and forms that work."}
        </p>
      )}

      {groups.length > 0 && (
        <div className="mk-groups" aria-label="Sections">
          {groups.map((g) => (
            <div key={g.key} className="mk-group">
              <span className="mk-group-title">{g.title}</span>
              <div className="mk-chips">
                {g.sections.map((s) => (
                  <button
                    key={s.id}
                    type="button"
                    className={"badge mk-chip" + (selected?.id === s.id && mode === "edit" ? " badge-warn" : "")}
                    aria-pressed={selected?.id === s.id && mode === "edit"}
                    onClick={() => pick(s)}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
