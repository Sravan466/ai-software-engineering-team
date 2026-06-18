"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, Project } from "@/lib/api";

const STATUS_BADGE: Record<string, string> = {
  created: "fd-badge-muted",
  running: "fd-badge-info",
  awaiting_approval: "fd-badge-accent",
  completed: "fd-badge-ok",
  failed: "fd-badge-warn",
};

export default function HomePage() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [idea, setIdea] = useState("");
  const [mode, setMode] = useState("local_only");
  const [requireApproval, setRequireApproval] = useState(true);
  const [routerStatus, setRouterStatus] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    try {
      setProjects(await api.listProjects());
      setRouterStatus(await api.routerStatus());
    } catch (e: any) {
      setError(e.message);
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
    <div className="grid gap-8 md:grid-cols-[1.4fr_1fr]">
      <section className="space-y-6">
        <div className="fd-card">
          <p className="fd-kicker mb-2">new build</p>
          <h1 className="fd-title text-2xl">
            Ship the whole team. <span className="fd-em">One idea in.</span>
          </h1>
          <p className="mb-4 mt-2 text-sm fd-muted">
            Describe a product idea. A team of AI agents takes it from requirements to a deployment
            plan, pausing for your approval at each phase.
          </p>
          <form onSubmit={onCreate} className="space-y-4">
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
              {busy ? "Creating…" : "Create project →"}
            </button>
          </form>
          {error && (
            <p className="mt-3 text-sm" style={{ color: "var(--accent)" }}>
              {error}
            </p>
          )}
        </div>

        <div className="fd-card">
          <div className="mb-3 flex items-center gap-3">
            <h2 className="fd-kicker">projects</h2>
            <span className="fd-rule" />
          </div>
          {projects.length === 0 ? (
            <p className="text-sm fd-dim">No projects yet.</p>
          ) : (
            <ul>
              {projects.map((p) => (
                <li
                  key={p.id}
                  style={{ borderColor: "var(--line)" }}
                  className="border-t py-3 first:border-0"
                >
                  <Link
                    href={`/projects/${p.id}`}
                    className="group flex items-center justify-between gap-4"
                  >
                    <span className="truncate text-sm transition-colors group-hover:text-[color:var(--accent)]">
                      {p.name || p.idea}
                    </span>
                    <span className={`fd-badge ${STATUS_BADGE[p.status] || "fd-badge-muted"}`}>
                      {p.status.replace("_", " ")}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>

      <aside className="space-y-4">
        <div className="fd-card">
          <div className="mb-3 flex items-center gap-3">
            <h2 className="fd-kicker">model providers</h2>
            <span className="fd-rule" />
          </div>
          {!routerStatus ? (
            <p className="text-sm fd-dim">Loading…</p>
          ) : (
            <ul className="space-y-2.5 text-sm">
              {Object.entries(routerStatus.providers).map(([name, info]: any) => (
                <li key={name} className="flex items-center justify-between">
                  <span className="capitalize fd-muted">
                    {name}
                    {info.is_local && <span className="ml-1 text-xs fd-dim">(local)</span>}
                  </span>
                  <span className={`fd-badge ${info.available ? "fd-badge-ok" : "fd-badge-muted"}`}>
                    {info.available ? "available" : "off"}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {routerStatus && (
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
            Product Requirements → Architecture → Backend → Frontend → Tests → Security → Deployment
            → Cost.
          </p>
        </div>
      </aside>
    </div>
  );
}
