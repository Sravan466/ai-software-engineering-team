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

// The composer's routing control. `label` is what a person reads, `backend` is
// what the API expects, and `hint` explains the trade-off in one line so the
// choice isn't three unexplained words.
export type RoutingModeMeta = {
  id: string;
  label: string;
  backend: string;
  hint: string;
  /** Manual is only a real choice once a model is pinned to the run — without one
   *  the router falls straight through to the auto chain, and the control does
   *  nothing at all. */
  needsModel?: boolean;
};

export const ROUTING_MODES: RoutingModeMeta[] = [
  {
    id: "local",
    label: "Local",
    backend: "local_only",
    hint: "Runs entirely on your own machine through Ollama. Free, private, slower.",
  },
  {
    id: "auto",
    label: "Auto",
    backend: "auto",
    hint: "Picks the best available model per phase, falling back to local when a cloud key is missing.",
  },
  {
    id: "manual",
    label: "Manual",
    backend: "manual",
    hint: "Pins one model to every phase of this run.",
    needsModel: true,
  },
];

// ── how often the run stops for you ─────────────────────────────────────────
// Gating every handoff identically is what turned review into a rubber stamp:
// the same card and the same two buttons whether an agent renamed a field or
// wrote the whole backend. The default gates on consequence instead.
export type ApprovalModeMeta = {
  id: ApprovalMode;
  label: string;
  /** The one line under the control. */
  hint: string;
  /** What the build view says is coming next, once the run is going. */
  running: string;
};

export const APPROVAL_MODES: ApprovalModeMeta[] = [
  {
    id: "checkpoints",
    label: "Two checkpoints",
    hint: "Stops twice — once on the plan, once on the finished build — and interrupts in between only for a severe security finding or a cost overrun.",
    running: "Stops on the plan and on the finished build.",
  },
  {
    id: "every_phase",
    label: "Every phase",
    hint: "Stops after all eight handoffs. Thorough, and eight decisions long.",
    running: "Stops after every one of the eight handoffs.",
  },
  {
    id: "unattended",
    label: "Unattended",
    hint: "Runs all eight phases end to end and hands you the result. Nothing interrupts it.",
    running: "Runs to the end without stopping.",
  },
];

export const APPROVAL_BY_ID: Record<string, ApprovalModeMeta> = Object.fromEntries(
  APPROVAL_MODES.map((m) => [m.id, m]),
);

// The phases each review covers. Mirrors PLAN_PHASES / SHIP_GATE_PHASE on the
// backend: a Plan review is one decision over Scope's spec and Atlas's
// architecture, and the Ship review is one pass over everything.
export const PLAN_PHASE_KEYS = ["product_manager", "system_design"];
