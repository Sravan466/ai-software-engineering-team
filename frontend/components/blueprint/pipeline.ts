/**
 * pipeline.ts — shared product model + run state machine.
 * TypeScript port of the Claude Design export `pipeline-engine.jsx`
 * (preserved verbatim at design/claude-design-export/).
 */
import { useEffect, useRef, useState } from "react";

export type Mode = "auto" | "local" | "manual";
export type RunStatus = "idle" | "thinking" | "gate" | "redo" | "done";
export type PhaseState = "pending" | "active" | "gate" | "redo" | "done";

export interface Phase {
  key: string;
  n: string;
  name: string;
  role: string;
  out: string;
  deliver: string;
  tok: number;
  heavy: boolean;
  debate?: boolean;
}

export interface Result {
  idx: number;
  model: string;
  cost: number;
  status: "auto" | "approved";
}

export const PHASES: Phase[] = [
  { key: "pm", n: "01", name: "Product Manager", role: "Scope & PRD",
    out: "PRD drafted — 6 user stories, 3 core flows, MVP cut line drawn.",
    deliver: "product-spec.md", tok: 1840, heavy: false },
  { key: "design", n: "02", name: "System Design", role: "Architecture",
    out: "Service split + data model — Postgres, 11 REST endpoints, auth boundary.",
    deliver: "architecture.md", tok: 2210, heavy: true },
  { key: "backend", n: "03", name: "Backend Engineer", role: "APIs & data", debate: true,
    out: "FastAPI services — SQLAlchemy models, streak + reminder logic, JWT auth.",
    deliver: "backend/", tok: 3120, heavy: true },
  { key: "frontend", n: "04", name: "Frontend Engineer", role: "Interface",
    out: "Next.js client — 9 routes, streak ring, reminder scheduler, settings.",
    deliver: "frontend/", tok: 2680, heavy: false },
  { key: "qa", n: "05", name: "QA Engineer", role: "Tests",
    out: "42 unit + 8 e2e — 87% coverage, 2 edge cases flagged for review.",
    deliver: "tests/", tok: 1560, heavy: false },
  { key: "sec", n: "06", name: "Security Engineer", role: "Threat model",
    out: "STRIDE pass — rate limiting + token rotation, 1 medium finding patched.",
    deliver: "security-review.md", tok: 1980, heavy: true },
  { key: "devops", n: "07", name: "DevOps Engineer", role: "CI / CD",
    out: "Dockerfile + GH Actions + compose — health checks and rollback wired.",
    deliver: ".github/ + Dockerfile", tok: 1340, heavy: false },
  { key: "cost", n: "08", name: "Cost Estimation", role: "Budget",
    out: "Run cost $0.041 — projected infra $18/mo, 86% of tokens ran local.",
    deliver: "cost-report.md", tok: 720, heavy: false },
];

export const DEBATE = {
  topic: "Database & persistence",
  parties: [
    { who: "System Design", stance: "Postgres — relational integrity for streak history." },
    { who: "Backend", stance: "SQLite for MVP — zero ops, ship faster." },
    { who: "Security", stance: "Whatever it is, encryption at rest is non-negotiable." },
  ],
  verdict: "Postgres + pgcrypto. SQLite revisit deferred to post-MVP.",
} as const;

export const EXAMPLES = [
  "A habit-tracking app with streaks and smart reminders",
  "A self-hosted read-it-later service with full-text search",
  "A Slack bot that summarizes channel threads on demand",
  "An invoicing tool for freelancers with Stripe payouts",
];

export const fmtCost = (n: number): string => (n === 0 ? "$0.00" : "$" + n.toFixed(3));
export const fmtTok = (n: number): string =>
  n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);

function resolveModel(phase: Phase, mode: Mode): string {
  if (mode === "local") return "qwen2.5:7b";
  if (mode === "manual") return "claude-sonnet";
  return phase.heavy ? "claude-sonnet" : "qwen2.5:7b"; // auto
}

function costFor(phase: Phase, model: string): number {
  return model.startsWith("qwen") ? 0 : +((phase.tok / 1000) * 0.009).toFixed(3);
}

export function usePipeline(initialIdea: string) {
  const [idea, setIdea] = useState(initialIdea);
  const [mode, setMode] = useState<Mode>("auto");
  const [approvals, setApprovals] = useState(true);
  const [status, setStatus] = useState<RunStatus>("idle");
  const [current, setCurrent] = useState(-1);
  const [results, setResults] = useState<Result[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function commitAndAdvance(result: Result) {
    setResults((r) => [...r, result]);
    const next = current + 1;
    if (next >= PHASES.length) {
      setCurrent(PHASES.length);
      setStatus("done");
    } else {
      setCurrent(next);
      setStatus("thinking");
    }
  }

  useEffect(() => {
    if (status !== "thinking") return;
    timer.current = setTimeout(() => {
      const phase = PHASES[current];
      if (approvals) {
        setStatus("gate");
      } else {
        const model = resolveModel(phase, mode);
        commitAndAdvance({ idx: current, model, cost: costFor(phase, model), status: "auto" });
      }
    }, 1000 + Math.random() * 600);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, current, approvals, mode]);

  function run() {
    if (timer.current) clearTimeout(timer.current);
    setResults([]);
    setCurrent(0);
    setStatus("thinking");
  }

  function approve() {
    if (status !== "gate") return;
    const phase = PHASES[current];
    const model = resolveModel(phase, mode);
    commitAndAdvance({ idx: current, model, cost: costFor(phase, model), status: "approved" });
  }

  function reject() {
    if (status !== "gate") return;
    setStatus("redo");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setStatus("gate"), 1300);
  }

  function reset() {
    if (timer.current) clearTimeout(timer.current);
    setResults([]);
    setCurrent(-1);
    setStatus("idle");
  }

  const totalTok = results.reduce((s, r) => s + PHASES[r.idx].tok, 0);
  const totalCost = results.reduce((s, r) => s + r.cost, 0);
  const localPct = results.length
    ? Math.round(
        (results.filter((r) => PHASES[r.idx]).filter((r) => r.model.startsWith("qwen")).length /
          results.length) *
          100
      )
    : 0;
  const progress = results.length / PHASES.length;

  return {
    PHASES,
    idea, setIdea,
    mode, setMode,
    approvals, setApprovals,
    status, current, results,
    run, approve, reject, reset,
    totals: { tok: totalTok, cost: totalCost, localPct, progress },
  };
}
