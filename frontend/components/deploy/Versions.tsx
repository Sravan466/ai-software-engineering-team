"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Version } from "@/lib/api";
import { Icon } from "@/components/shell/icons";
import { ago, useNow } from "@/components/setup/parts";
import { ChangeDiff } from "@/components/build/Changes";

/**
 * Every version of the build (#79), newest first, inside the card that takes a copy:
 * download any of them, see what each changed, and bring an earlier one back — as the
 * newest version, so nothing that came after it is lost.
 *
 * Restoring is refused while a change is open or the crew is working: the build has
 * one current version, and it is the one a change is made on.
 */
export default function Versions({
  id,
  status,
  changeOpen,
  currentKey,
  onRestored,
}: {
  id: string;
  /** The build's status — restoring needs it finished. */
  status: string;
  changeOpen: boolean;
  /** Changes whenever the current version does, so the list follows it. */
  currentKey: unknown;
  onRestored: () => void;
}) {
  const [list, setList] = useState<Version[] | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [restoring, setRestoring] = useState<number | null>(null);
  const now = useNow(30000);

  const load = useCallback(() => {
    api
      .listVersions(id)
      .then((r) => {
        setList(r.versions);
        setError("");
      })
      .catch((e: any) => setError(e.message));
  }, [id]);

  useEffect(() => {
    load();
  }, [load, currentKey]);

  async function restore(n: number) {
    setRestoring(n);
    setError("");
    try {
      await api.restoreVersion(id, n);
      onRestored();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setRestoring(null);
    }
  }

  if (error && !list) {
    return (
      <p className="field-error" role="alert">
        {Icon.alert}
        <span>Couldn&apos;t load the versions: {error}</span>
      </p>
    );
  }
  if (!list || list.length === 0) return null;

  const blocked =
    changeOpen
      ? "Keep or discard the change in progress first."
      : status !== "completed"
        ? "Restoring is ready once the crew has finished."
        : "";

  return (
    <div className="versions">
      <div className="versions-head">
        <h3 className="label">Versions</h3>
        <span className="field-hint">
          Every change you keep is a version. Restoring one makes it the newest, so nothing after it is lost.
        </span>
      </div>
      {error && (
        <p className="field-error" role="alert">
          {Icon.alert}
          <span>{error}</span>
        </p>
      )}
      <ol className="version-list">
        {list.map((v) => {
          const expanded = open === v.number;
          return (
            <li key={v.id} className="version-row" data-current={v.current || undefined}>
              <div className="version-line">
                <span className="version-num mono">v{v.number}</span>
                <div className="version-main">
                  <span className="version-label">{v.label}</span>
                  <span className="version-meta">
                    {ago(v.created_at, now)} · {v.files} file{v.files === 1 ? "" : "s"}
                  </span>
                </div>
                <span className="version-tags">
                  {v.current && <span className="badge badge-ok">Current</span>}
                  {v.deployed && <span className="badge">Deployed</span>}
                  {v.pushed && <span className="badge">{Icon.github} Pushed</span>}
                </span>
                <div className="version-actions">
                  <button
                    type="button"
                    className="btn btn-sm btn-ghost"
                    aria-expanded={expanded}
                    onClick={() => setOpen(expanded ? null : v.number)}
                  >
                    What changed
                  </button>
                  <a className="btn btn-sm" href={api.downloadUrl(id, false, v.number)} download>
                    {Icon.download}
                    <span className="sr-only">Download v{v.number}</span>
                  </a>
                  {!v.current && (
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={Boolean(blocked) || restoring !== null}
                      title={blocked || `Bring v${v.number} back as the newest version`}
                      onClick={() => restore(v.number)}
                    >
                      {restoring === v.number ? <span className="btn-spinner" aria-hidden="true" /> : Icon.rotate}
                      Restore
                    </button>
                  )}
                </div>
              </div>
              {expanded && (
                <div className="version-diff">
                  <ChangeDiff
                    load={() => api.versionDiff(id, v.number)}
                    reloadKey={v.number}
                    empty={v.number === 1 ? "The first build." : "Nothing in the code changed."}
                  />
                </div>
              )}
            </li>
          );
        })}
      </ol>
      {blocked && list.length > 1 && <p className="field-hint versions-foot">{blocked}</p>}
    </div>
  );
}
