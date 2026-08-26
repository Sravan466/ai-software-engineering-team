"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { api, type LocalStatus, type Project } from "@/lib/api";
import { Icon } from "./icons";
import { Skeleton } from "@/components/ui/Skeleton";

// Status → the leading dot on each recent-build row.
const DOT: Record<string, string> = {
  completed: "dot-ok",
  awaiting_approval: "dot-warn dot-pulse",
  running: "dot-run dot-pulse",
  failed: "dot-bad",
};

const STATUS_TEXT: Record<string, string> = {
  completed: "Completed",
  awaiting_approval: "Waiting for your approval",
  running: "Running",
  failed: "Failed",
  created: "Not started",
};

// Compact relative time ("3h", "2d") for the recent-builds list.
function timeAgo(iso: string): string {
  const then = +new Date(iso);
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, (Date.now() - then) / 1000);
  if (secs < 60) return "now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d`;
  return new Date(then).toLocaleDateString();
}

const API_DOCS_URL =
  (process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000") + "/docs";

/**
 * The persistent left rail — and, under 900px, an overlay drawer.
 *
 * When collapsed, shell.css flips it to `visibility: hidden`, which also takes
 * it out of the tab order — otherwise keyboard focus disappears into an
 * off-screen drawer.
 */
export default function Sidebar({ onClose }: { onClose: () => void }) {
  const pathname = usePathname();
  const router = useRouter();
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [local, setLocal] = useState<LocalStatus | null>(null);
  const [localError, setLocalError] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setProjects(await api.listProjects());
    } catch {
      // Keep the last good list if the backend blips.
      setProjects((p) => p ?? []);
    }
    try {
      setLocal(await api.getLocalModel());
      setLocalError(false);
    } catch {
      setLocalError(true);
    }
  }, []);

  // Cheap, and keeps the list fresh after a build is created or advances.
  useEffect(() => {
    refresh();
  }, [refresh, pathname]);

  const ready = !localError && local?.reachable && local?.has_default;
  const runtimeLabel = localError
    ? "Backend unreachable"
    : !local?.reachable
      ? "Ollama not running"
      : !local.has_default
        ? `${local.default_model} not downloaded`
        : `${local.default_model} ready`;

  return (
    <aside className="sidebar">
      <nav aria-label="Builds">
        <div className="sb-top">
          <Link className="sb-brand" href="/">
            <span className="logo-mark" aria-hidden="true">
              AI
            </span>
            <span className="wordmark">
              SWE&nbsp;<span>Team</span>
            </span>
          </Link>
          <button className="icon-btn" onClick={onClose} aria-label="Close navigation">
            {Icon.menu}
          </button>
        </div>

        <button className="sb-new" onClick={() => router.push("/")}>
          {Icon.plus} New build
        </button>
      </nav>

      <div className="sb-scroll">
        <h2 className="label sb-label">Recent builds</h2>

        {projects === null ? (
          <div style={{ padding: "4px 20px", display: "flex", flexDirection: "column", gap: 12 }}>
            <Skeleton w="80%" />
            <Skeleton w="64%" />
            <Skeleton w="72%" />
          </div>
        ) : projects.length === 0 ? (
          <p className="sb-empty">
            No builds yet. Describe an idea on the home screen to start one.
          </p>
        ) : (
          projects.map((p) => {
            const active = pathname === `/projects/${p.id}`;
            const title = p.name || p.idea;
            return (
              <Link
                key={p.id}
                href={`/projects/${p.id}`}
                className="sb-item"
                aria-current={active ? "page" : undefined}
              >
                <span
                  className={"dot " + (DOT[p.status] || "")}
                  title={STATUS_TEXT[p.status] || p.status}
                />
                <span className="sb-item-title">{title}</span>
                <span className="sb-item-time">{timeAgo(p.updated_at || p.created_at)}</span>
              </Link>
            );
          })
        )}
      </div>

      <div className="sb-foot">
        {/* Nothing can be built while the local runtime is down, so this is a
            link to the fix rather than a caption about the problem. */}
        <Link
          className={"sb-runtime" + (ready ? "" : " down")}
          href="/settings"
          title={local?.base_url}
        >
          <span className={"dot " + (ready ? "dot-ok" : "dot-warn")} />
          <span className="sb-runtime-text">{runtimeLabel}</span>
          {!ready && <span className="sb-runtime-fix">Fix</span>}
        </Link>

        <Link
          className="sb-link"
          href="/crew"
          aria-current={pathname === "/crew" ? "page" : undefined}
        >
          {Icon.sparkle} The crew
        </Link>
        <Link
          className="sb-link"
          href="/settings"
          aria-current={pathname === "/settings" ? "page" : undefined}
        >
          {Icon.gear} Settings
        </Link>
        <a className="sb-link" href={API_DOCS_URL} target="_blank" rel="noreferrer">
          {Icon.api} API reference
          <span style={{ marginLeft: "auto", color: "var(--ink-4)" }}>{Icon.external}</span>
        </a>
      </div>
    </aside>
  );
}
