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

export const PHASES: PhaseMeta[] = [
  { key: "product_manager", n: "01", name: "Product Manager", label: "Requirements", role: "Scope & PRD", agent: "pm-agent", deliver: "product-spec.md" },
  { key: "system_design", n: "02", name: "System Design", label: "Architecture", role: "Architecture", agent: "arch-agent", deliver: "architecture.md" },
  { key: "backend_engineer", n: "03", name: "Backend Engineer", label: "Backend", role: "APIs & data", agent: "be-agent", deliver: "backend/", debate: true },
  { key: "frontend_engineer", n: "04", name: "Frontend Engineer", label: "Frontend", role: "Interface", agent: "fe-agent", deliver: "frontend/" },
  { key: "qa_engineer", n: "05", name: "QA Engineer", label: "Tests", role: "Tests", agent: "qa-agent", deliver: "tests/" },
  { key: "security_engineer", n: "06", name: "Security Engineer", label: "Security", role: "Threat model", agent: "sec-agent", deliver: "security-review.md" },
  { key: "devops_engineer", n: "07", name: "DevOps Engineer", label: "Deployment", role: "CI / CD", agent: "ops-agent", deliver: ".github/ + Dockerfile" },
  { key: "cost_estimation", n: "08", name: "Cost Estimation", label: "Cost", role: "Budget", agent: "cost-agent", deliver: "cost-report.md" },
];

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
