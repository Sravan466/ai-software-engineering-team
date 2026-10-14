import { AGENTS } from "@/components/agents/personas";
import type { ApprovalMode } from "@/lib/api";

// The eight pipeline phases, in order. Mirrors the backend agent keys
// (app/agents/*) and the v3 design's phase metadata. Shared by the sidebar,
// the New Build composer (pipeline footer) and the project/build view.
export type PhaseMeta = {
  key: string; // backend phase key
  n: string; // "01".."08"
  name: string; // agent role name, shown on the live pipeline node
  label: string; // short label, shown on the tracker + composer footer
  role: string; // role tag
  agent: string; // agent id
  deliver: string; // primary deliverable (Summary → deliverables)
  debate?: boolean; // phases that debate before settling
};

export const PHASES: PhaseMeta[] = AGENTS.map((a) => ({
  key: a.key,
  n: a.n,
  name: a.role,
  label: a.discipline,
  role: a.discipline,
  agent: a.codename,
  deliver: a.deliver,
  debate: a.debate,
}));

export const PHASE_LABELS: Record<string, string> = Object.fromEntries(
  PHASES.map((p) => [p.key, p.label]),
);
export const PHASE_BY_KEY: Record<string, PhaseMeta> = Object.fromEntries(
  PHASES.map((p) => [p.key, p]),
);

// Map the suggestion chips on the New Build composer (kept here so the home page
// stays focused on wiring).
export const EXAMPLES = [
  "A habit-tracking app with streaks and smart reminders",
  "A self-hosted read-it-later service with full-text search",
  "A Slack bot that summarizes channel threads on demand",
  "An invoicing tool for freelancers with Stripe payouts",
];

// The composer's model control (#96). `label` is what a person reads, `backend` is
// what the API expects. What each one runs is said with the real model names, so
// the description is built where those are known (`RunSettings`); `hint` is the
// line used when they aren't.
export type RoutingModeMeta = {
  id: string;
  label: string;
  backend: string;
  hint: string;
  /** Pick a model is only a real choice once a model is pinned to the run — without
   *  one the router falls straight through to the auto chain, and the control does
   *  nothing at all. */
  needsModel?: boolean;
};

export const ROUTING_MODES: RoutingModeMeta[] = [
  {
    id: "auto",
    label: "Auto",
    backend: "auto",
    hint: "Picks each agent's model by how each one did in your past builds.",
  },
  {
    id: "local",
    label: "Local model",
    backend: "local_only",
    hint: "Every agent uses your default model on this computer. Free, and nothing leaves your machine.",
  },
  {
    id: "manual",
    label: "Pick a model",
    backend: "manual",
    hint: "Every agent uses the model you choose.",
    needsModel: true,
  },
];

export const ROUTING_BY_BACKEND: Record<string, RoutingModeMeta> = Object.fromEntries(
  ROUTING_MODES.map((m) => [m.backend, m]),
);

// ── when the run checks with you ─────────────────────────────────────────────
// One copy for the composer and the build page's popover, so the two can't drift.
// Every description has to be true of what the runner does (`decide_gate`,
// `gate_for`): the database and connector questions are asked in every mode, and a
// build that contradicts its stack, doesn't build, fails its tests or needs help
// stops even when nobody asked it to.
export type ApprovalModeMeta = {
  id: ApprovalMode;
  label: string;
  /** The line under the option. */
  hint: string;
  /** The short form in the composer's collapsed line. */
  summary: string;
  /** What the build view says is coming next, once the run is going. */
  running: string;
};

export const APPROVAL_MODES: ApprovalModeMeta[] = [
  {
    id: "checkpoints",
    label: "At the plan and the end",
    hint:
      "You approve the plan before any code is written, and the finished app before it ships. " +
      "It also asks when a security finding or the hosting cost needs your call.",
    summary: "Checks at the plan and the end",
    running: "Checks with you at the plan and at the end.",
  },
  {
    id: "every_phase",
    label: "After each agent",
    hint: "You review each of the 8 agents' work before the next one starts.",
    summary: "Checks after each agent",
    running: "Checks with you after each agent.",
  },
  {
    id: "unattended",
    label: "Only when it's stuck",
    hint:
      "Runs to the end on its own. It still stops to ask for what only you can give, like a " +
      "database or a service's API key. It also stops when it can't fix something itself: code " +
      "that won't build, failing tests, a part written for a different stack, or a serious " +
      "security problem.",
    summary: "Checks only when stuck",
    running: "Runs to the end unless it gets stuck.",
  },
];

/** The cost cap, worded once for both surfaces. */
export const COST_CAP_COPY = {
  label: "Ask me if hosting the app would cost more than",
  suffix: "a month",
  hint:
    "Ledger, the budget agent, estimates servers, the database and paid APIs from the plan. " +
    "This cap doesn't cover what the build spends on AI.",
};

export const APPROVAL_BY_ID: Record<string, ApprovalModeMeta> = Object.fromEntries(
  APPROVAL_MODES.map((m) => [m.id, m]),
);

// The phases each review covers. Mirrors PLAN_PHASES / SHIP_GATE_PHASE on the
// backend: a Plan review is one decision over Scope's spec and Atlas's
// architecture, and the Ship review is one pass over everything.
export const PLAN_PHASE_KEYS = ["product_manager", "system_design"];
