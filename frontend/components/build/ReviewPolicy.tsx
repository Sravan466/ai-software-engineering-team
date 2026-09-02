"use client";

import { useEffect, useRef, useState } from "react";
import { api, type ApprovalMode, type Project } from "@/lib/api";
import { APPROVAL_BY_ID, APPROVAL_MODES } from "@/components/shell/phases";
import { Icon } from "@/components/shell/icons";

/**
 * How often this run stops — changeable while it is running.
 *
 * `require_approval` was fixed at create time: a build heading somewhere expensive
 * could not be given a gate, and one you had come to trust could not be let off its
 * leash without starting over. The runner re-reads the policy before every handoff,
 * so a change here lands on the next one.
 */
export default function ReviewPolicy({
  project,
  id,
  onChanged,
}: {
  project: Project;
  id: string;
  /** Reload the project so the badge and the next gate agree immediately. */
  onChanged: () => void | Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cap, setCap] = useState(
    project.cost_cap_usd === null ? "" : String(project.cost_cap_usd),
  );

  // The page polls without remounting this, so a cap saved elsewhere would otherwise
  // sit here as a stale number waiting to be written back over the newer one. Only
  // resyncs when the project's value actually differs from what is in the field, so
  // it never snatches a number out from under someone typing.
  useEffect(() => {
    const live = project.cost_cap_usd;
    setCap((text) => (Number(text) === live ? text : live === null ? "" : String(live)));
  }, [project.cost_cap_usd]);

  // It floats over the page, so it dismisses the way a floating thing is expected
  // to: Escape from anywhere inside it, or a click outside. Escape returns focus to
  // the control that opened it rather than dropping the keyboard at the page root.
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      toggle.current?.focus();
    };
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open]);

  const current = APPROVAL_BY_ID[project.approval_mode] ?? APPROVAL_MODES[0];
  const settled =
    project.status === "completed" ||
    project.status === "failed" ||
    project.status === "cancelled";

  async function save(body: Parameters<typeof api.updateProject>[1]) {
    setBusy(true);
    setError("");
    try {
      await api.updateProject(id, body);
      await onChanged();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  function saveCap() {
    const raw = cap.trim();
    if (raw === "") return save({ clear_cost_cap: true });
    const value = Number(raw);
    if (!Number.isFinite(value) || value <= 0) {
      // Zero is not a cap you can be under, and the API rejects it — say so here
      // rather than letting a 422 come back for something with an obvious meaning.
      setError("Enter an amount above zero, or clear the field for no cap.");
      return;
    }
    return save({ cost_cap_usd: value });
  }

  return (
    <div className="policy" ref={root}>
      <button
        type="button"
        ref={toggle}
        className="badge policy-toggle"
        aria-expanded={open}
        aria-controls="review-policy"
        onClick={() => setOpen((o) => !o)}
        disabled={settled}
        title={settled ? "This run has finished" : "Change when this build stops for you"}
      >
        {current.label}
        <span className={"phase-chev" + (open ? " open" : "")} aria-hidden="true">
          {Icon.chevron}
        </span>
      </button>

      {open && (
        <div id="review-policy" className="policy-panel">
          <div className="setting">
            <span className="label" id="policy-label">
              When this build stops for you
            </span>
            <div className="seg" role="group" aria-labelledby="policy-label">
              {APPROVAL_MODES.map((m) => (
                <button
                  key={m.id}
                  type="button"
                  className="seg-btn"
                  aria-pressed={project.approval_mode === m.id}
                  disabled={busy}
                  onClick={() => save({ approval_mode: m.id as ApprovalMode })}
                >
                  {m.label}
                </button>
              ))}
            </div>
            <p className="field-hint">{current.hint}</p>
          </div>

          {project.approval_mode === "checkpoints" && (
            <div className="setting">
              <label className="label" htmlFor="policy-cap">
                Interrupt over a projected monthly cost
              </label>
              <div className="prefixed">
                <span className="prefixed-mark" aria-hidden="true">
                  $
                </span>
                <input
                  id="policy-cap"
                  className="input input-mono"
                  inputMode="decimal"
                  placeholder="no cap"
                  value={cap}
                  disabled={busy}
                  onChange={(e) => setCap(e.target.value.replace(/[^0-9.]/g, ""))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") saveCap();
                  }}
                />
                <span className="prefixed-suffix">/month</span>
                <button className="btn btn-sm" disabled={busy} onClick={saveCap}>
                  {busy && <span className="btn-spinner" aria-hidden="true" />}
                  Save
                </button>
              </div>
            </div>
          )}

          {error && (
            <p className="field-hint" role="alert" style={{ color: "var(--bad)" }}>
              {error}
            </p>
          )}
          {project.status === "awaiting_approval" && project.approval_mode === "unattended" && (
            <p className="field-hint">
              This build is still parked on the decision below. Approve it once and it
              will run to the end.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
