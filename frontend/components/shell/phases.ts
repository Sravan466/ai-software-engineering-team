import { AGENTS } from "@/components/agents/personas";

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
export const ROUTING_MODES: { id: string; label: string; backend: string; hint: string }[] = [
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
    hint: "Uses the model you pinned in Settings for every phase.",
  },
];
