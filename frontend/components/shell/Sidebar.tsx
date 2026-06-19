"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { api, type LocalStatus, type Project } from "@/lib/api";
import { Icon } from "./icons";

// Status → the little leading dot on each recent-build row.
const DOT_CLASS: Record<string, string> = {
  completed: "fd-dot-ok",
  awaiting_approval: "fd-dot-accent",
  running: "fd-dot-accent",
};

// Compact relative time for the recent-builds list ("3h", "2d").
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
 * The persistent left rail. Brand + "New build" both go home (the composer);
 * RECENT BUILDS lists real projects (live from the backend) and links into each
 * build; the footer shows the Ollama status and links to Settings / API docs.
 * Re-fetches on navigation so a just-created build appears without a reload.
 */
export default function Sidebar({ onToggle }: { onToggle: () => void }) {
  const pathname = usePathname();
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [local, setLocal] = useState<LocalStatus | null>(null);
  const [localError, setLocalError] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setProjects(await api.listProjects());
    } catch {
      /* keep the last good list if the backend blips */
    }
    try {
      setLocal(await api.getLocalModel());
      setLocalError(false);
    } catch {
      setLocalError(true);
    }
  }, []);

  // Re-run on every route change — cheap, and keeps the list fresh after a build
  // is created or its status advances.
  useEffect(() => {
    refresh();
  }, [refresh, pathname]);

  const ollamaReady = !localError && local?.reachable;

  return (
    <aside className="sidebar">
      <div className="sb-top">
        <Link className="sb-brand" href="/" title="New build">
          <span className="fd-logo-mark" />
          <span className="fd-wordmark">
            AI<span>SWE TEAM</span>
          </span>
        </Link>
        <button className="icon-btn" onClick={onToggle} title="Collapse sidebar" aria-label="Collapse sidebar">
          {Icon.menu}
        </button>
      </div>

      <button className="sb-new" onClick={() => router.push("/")}>
        {Icon.plus} New build
      </button>

      <div className="sb-scroll">
        <div className="sb-label">Recent builds</div>
        {projects.length === 0 ? (
          <p className="sb-empty">No builds yet — describe an idea to start one.</p>
        ) : (
          projects.map((p) => {
            const active = pathname === `/projects/${p.id}`;
            return (
              <Link
                key={p.id}
                href={`/projects/${p.id}`}
                className={"sb-item" + (active ? " on" : "")}
                title={p.name || p.idea}
              >
                <span className={"fd-dot sb-item-dot " + (DOT_CLASS[p.status] || "")} />
                <span className="sb-item-title">{p.name || p.idea}</span>
                <span className="sb-item-time">{timeAgo(p.updated_at || p.created_at)}</span>
              </Link>
            );
          })
        )}
      </div>

      <div className="sb-foot">
        <div className="sb-status">
          <span className={"fd-dot " + (ollamaReady ? "fd-dot-ok" : "")} />
          {ollamaReady ? "ollama · local ready" : "ollama · offline"}
        </div>
        <Link className={"sb-link" + (pathname === "/settings" ? " on" : "")} href="/settings">
          {Icon.gear} Settings
        </Link>
        <a className="sb-link" href={API_DOCS_URL} target="_blank" rel="noreferrer">
          {Icon.api} API docs
        </a>
      </div>
    </aside>
  );
}
