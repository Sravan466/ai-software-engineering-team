"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, type Project } from "@/lib/api";
import { Icon } from "@/components/shell/icons";

/**
 * Stop, Resume, Delete — the ways out of a run.
 *
 * Before this there were none. Every control 400'd on a `running` project, so a run
 * that died with its server sat at `running` forever while the page polled it every
 * 2.5 seconds; `DELETE /api/projects/{id}` existed and nothing in the UI called it.
 *
 * Which control shows is derived from the status, so the surface never offers an
 * action the backend will refuse:
 *
 *   running · awaiting_approval → Stop      (work already done is kept)
 *   cancelled · failed · stalled → Resume   (from the last checkpoint)
 *   any state                    → Delete   (two-step, never a modal)
 */
export default function RunControls({
  project,
  busy,
  act,
}: {
  project: Project;
  busy: boolean;
  act: (fn: () => Promise<unknown>) => Promise<boolean>;
}) {
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const { id, status, stalled } = project;
  const inFlight = status === "running" && !stalled;
  const canStop = status === "running" || status === "awaiting_approval";
  const canResume = stalled || status === "cancelled" || status === "failed";

  async function remove() {
    setDeleting(true);
    try {
      await api.deleteProject(id);
      router.push("/");
      router.refresh();
    } catch {
      setDeleting(false);
      setConfirming(false);
    }
  }

  if (confirming) {
    return (
      <div
        className="confirm"
        role="group"
        aria-label="Confirm deleting this build"
        onKeyDown={(e) => {
          if (e.key === "Escape") setConfirming(false);
        }}
      >
        <span className="confirm-text">
          Delete this build and everything it generated?
        </span>
        {/* Focus lands on the safe choice: this is irreversible, and a stray Enter
            on a focused destructive button is not a confirmation. */}
        <button
          className="btn btn-sm"
          disabled={deleting}
          onClick={() => setConfirming(false)}
          autoFocus
        >
          Keep it
        </button>
        <button className="btn btn-sm btn-danger" disabled={deleting} onClick={remove}>
          {deleting && <span className="btn-spinner" aria-hidden="true" />}
          {deleting ? "Deleting…" : "Delete permanently"}
        </button>
      </div>
    );
  }

  return (
    <>
      {canStop && (
        <button
          className="btn"
          disabled={busy}
          onClick={() => act(() => api.stop(id))}
          title={
            inFlight
              ? "The current model call finishes, then the pipeline halts."
              : "Pause this build. Nothing already approved is lost."
          }
        >
          {Icon.stop} Stop
        </button>
      )}
      {canResume && (
        <button
          className="btn btn-primary"
          disabled={busy}
          onClick={() => act(() => api.resume(id))}
        >
          {busy && <span className="btn-spinner" aria-hidden="true" />}
          {Icon.play} Resume
        </button>
      )}
      <button
        className="icon-btn icon-btn-danger"
        aria-label="Delete this build"
        title="Delete this build"
        onClick={() => setConfirming(true)}
      >
        {Icon.trash}
      </button>
    </>
  );
}
