"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type BuildRunnerStatus } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { SkeletonLines } from "@/components/ui/Skeleton";

/**
 * Settings → Build runner (#75): what installs, builds and starts the generated code
 * before Ship — Docker on this computer, the builder service, or nothing.
 *
 * With nothing, code is still parsed and its imports checked; it just isn't built,
 * and this row says why in the words the backend used. Nothing here starts a build.
 */
export default function BuildRunnerSettings() {
  const [state, setState] = useState<BuildRunnerStatus | null>(null);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);

  const load = useCallback(async () => {
    setChecking(true);
    try {
      setState(await api.getBuildRunner());
      setError("");
    } catch (e: any) {
      setError(e?.message || "The backend didn't answer.");
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="card" aria-labelledby="build-runner-title">
      <div className="sec-head">
        <h2 className="label" id="build-runner-title">
          Build runner
        </h2>
        <span className="rule" />
      </div>
      <p className="ship-copy" style={{ marginBottom: 14 }}>
        Before Ship, the crew installs, builds and starts each code phase in a throwaway container with no
        network once packages are in. What fails goes back to the crew.
      </p>
      {!state && !error && <SkeletonLines lines={1} />}
      {error && (
        <div className="notice notice-bad" role="alert">
          {Icon.alert}
          <div className="notice-body">
            <span className="notice-title">Couldn&apos;t check the build runner</span>
            <span className="notice-text">{error}</span>
          </div>
        </div>
      )}
      {state && (
        <div className="ship-conn runner-row" data-ok={state.available || undefined} aria-live="polite">
          <span className="ship-conn-mark runner-mark" aria-hidden="true">
            {state.available ? Icon.check : Icon.info}
          </span>
          <span className="ship-conn-text">
            <b>{title(state)}</b>
            {state.available && state.detail && (
              <>
                {" · "}
                <span className="mono">{state.detail}</span>
              </>
            )}
            <span className={`badge ${state.available ? "badge-ok" : ""}`}>
              {state.available ? "Builds code" : "Parses only"}
            </span>
            {!state.available && state.reason && <span className="runner-reason">{state.reason}</span>}
            {state.available && state.images && state.images.length > 0 && (
              <span className="runner-reason">
                Runs in <span className="mono">{state.images.join(", ")}</span>
              </span>
            )}
          </span>
          <span className="ship-conn-actions">
            <button type="button" className="btn btn-sm btn-ghost" onClick={load} disabled={checking}>
              {checking ? <span className="btn-spinner" aria-hidden="true" /> : Icon.refresh}
              Check again
            </button>
          </span>
        </div>
      )}
    </section>
  );
}

function title(s: BuildRunnerStatus): string {
  if (s.kind === "off") return "Switched off";
  if (s.kind === "builder") return s.available ? "Builder service" : "Builder service unavailable";
  if (s.kind === "docker") return "Docker on this computer";
  return "Not available";
}
