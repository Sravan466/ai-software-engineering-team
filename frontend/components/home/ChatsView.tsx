"use client";

import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, Project } from "@/lib/api";
import "../blueprint/blueprint.css";
import "./home.css";

const STATUS_BADGE: Record<string, string> = {
  created: "fd-badge-muted",
  running: "fd-badge-info",
  awaiting_approval: "fd-badge-accent",
  completed: "fd-badge-ok",
  failed: "fd-badge-warn",
};

// Short relative time for the previous-chats list ("3h ago", "2d ago").
function timeAgo(iso: string): string {
  const then = +new Date(iso);
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, (Date.now() - then) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(then).toLocaleDateString();
}

/**
 * Chats tab of the merged home page. Lists previous chats (projects) and lets
 * the user start a new one. Carries the same Blueprint chrome as the Home tab
 * (it renders its own `.fd-root` skin + header) so switching tabs feels seamless.
 */
export default function ChatsView({ nav }: { nav: ReactNode }) {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [idea, setIdea] = useState("");
  const [mode, setMode] = useState("local_only");
  const [requireApproval, setRequireApproval] = useState(true);
  const [routerStatus, setRouterStatus] = useState<any>(null);
  const [statusError, setStatusError] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    // Fetch independently so one failure doesn't leave the other stuck loading.
    try {
      setProjects(await api.listProjects());
    } catch (e: any) {
      setError(e.message);
    }
    try {
      setStatusError("");
      setRouterStatus(await api.routerStatus());
    } catch (e: any) {
      setStatusError(e.message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function onCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!idea.trim()) return;
    setBusy(true);
    setError("");
    try {
      const project = await api.createProject({
        idea,
        routing_mode: mode,
        require_approval: requireApproval,
      });
      router.push(`/projects/${project.id}`);
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  }

  return (
    <div
      className="fd-root"
      data-theme="dark"
      data-palette="rgb"
      data-font="brut"
      data-density="comfy"
      data-grain="on"
      data-scan="on"
      data-cursor="off"
    >
      <div className="dir fx-skin">
        <div className="chats">
          <header className="bp-head chats-head">
            <div className="bp-wm">
              <span className="bp-wm-mark" />
              <span className="bp-wm-text">
                AI<span className="bp-wm-thin">SWE TEAM</span>
              </span>
            </div>
            <div className="bp-head-meta">{nav}</div>
          </header>

          <div className="chats-body">
            <div className="chats-intro">
              <h1>
                Your chats. <span className="chats-intro-em">One idea in.</span>
              </h1>
              <p>
                Each chat is a build: describe a product idea and a team of AI agents takes it from
                requirements to a deployment plan, pausing for your approval at every phase.
              </p>
            </div>

            <div className="chats-grid">
              {/* LEFT — new chat + history */}
              <section className="space-y-4">
                <div className="fd-card">
                  <p className="fd-kicker mb-2">new chat</p>
                  <h2 className="fd-title text-xl">Start a new build</h2>
                  <form onSubmit={onCreate} className="mt-4 space-y-4">
                    <textarea
                      className="fd-textarea h-28 w-full"
                      placeholder="e.g. Build a food delivery platform for college students."
                      value={idea}
                      onChange={(e) => setIdea(e.target.value)}
                    />
                    <div className="flex flex-wrap items-center gap-4 text-sm">
                      <label className="flex items-center gap-2">
                        <span className="fd-kicker">routing</span>
                        <select
                          className="fd-select w-auto"
                          value={mode}
                          onChange={(e) => setMode(e.target.value)}
                        >
                          <option value="local_only">Local only (Ollama)</option>
                          <option value="auto">Auto</option>
                          <option value="manual">Manual</option>
                        </select>
                      </label>
                      <label className="flex items-center gap-2 fd-muted">
                        <input
                          type="checkbox"
                          className="accent-[color:var(--accent)]"
                          checked={requireApproval}
                          onChange={(e) => setRequireApproval(e.target.checked)}
                        />
                        <span>Require approval each phase</span>
                      </label>
                    </div>
                    <button className="fd-btn fd-btn-primary" disabled={busy}>
                      {busy ? "Starting…" : "Start chat →"}
                    </button>
                  </form>
                  {error && (
                    <p className="mt-3 text-sm" style={{ color: "var(--accent)" }}>
                      {error}
                    </p>
                  )}
                </div>

                <div className="fd-card">
                  <div className="mb-2 flex items-center gap-3">
                    <h2 className="fd-kicker">previous chats</h2>
                    <span className="fd-rule" />
                    <span className="fd-kicker">{projects.length}</span>
                  </div>
                  {projects.length === 0 ? (
                    <p className="py-2 text-sm fd-dim">No chats yet — start one above.</p>
                  ) : (
                    <div>
                      {projects.map((p) => (
                        <Link key={p.id} href={`/projects/${p.id}`} className="chat-row">
                          <span className="chat-row-main">
                            <span className="chat-row-dot" />
                            <span className="chat-row-text">
                              <span className="chat-row-title">{p.name || p.idea}</span>
                              <span className="chat-row-meta">
                                {p.routing_mode} · {timeAgo(p.updated_at || p.created_at)}
                              </span>
                            </span>
                          </span>
                          <span className={`fd-badge ${STATUS_BADGE[p.status] || "fd-badge-muted"}`}>
                            {p.status.replace("_", " ")}
                          </span>
                        </Link>
                      ))}
                    </div>
                  )}
                </div>
              </section>

              {/* RIGHT — providers + pipeline */}
              <aside className="space-y-4">
                <div className="fd-card">
                  <div className="mb-3 flex items-center gap-3">
                    <h2 className="fd-kicker">model providers</h2>
                    <span className="fd-rule" />
                  </div>
                  {statusError ? (
                    <div className="text-sm">
                      <p style={{ color: "var(--accent)" }}>Couldn&apos;t reach the backend.</p>
                      <button className="fd-btn fd-btn-sm mt-2" onClick={refresh}>
                        Retry
                      </button>
                    </div>
                  ) : !routerStatus ? (
                    <p className="text-sm fd-dim">Loading…</p>
                  ) : (
                    <ul className="space-y-2.5 text-sm">
                      {Object.entries(routerStatus.providers).map(([name, info]: any) => (
                        <li key={name} className="flex items-center justify-between">
                          <span className="capitalize fd-muted">
                            {name}
                            {info.is_local && <span className="ml-1 text-xs fd-dim">(local)</span>}
                          </span>
                          <span
                            className={`fd-badge ${info.available ? "fd-badge-ok" : "fd-badge-muted"}`}
                          >
                            {info.available ? "available" : "off"}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                  {routerStatus && !statusError && (
                    <p className="mt-3 text-xs fd-dim">
                      Default mode: {routerStatus.default_mode} · Fallback:{" "}
                      {routerStatus.fallback_chain.join(" → ")}
                    </p>
                  )}
                  <Link href="/settings" className="fd-link mt-3 inline-block text-xs">
                    Manage models &amp; keys →
                  </Link>
                </div>

                <div className="fd-card">
                  <div className="mb-3 flex items-center gap-3">
                    <h3 className="fd-kicker">pipeline</h3>
                    <span className="fd-rule" />
                  </div>
                  <p className="text-sm fd-muted">
                    Product Requirements → Architecture → Backend → Frontend → Tests → Security →
                    Deployment → Cost.
                  </p>
                </div>
              </aside>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
