# Architecture

## Big picture

```
┌──────────────┐      REST/JSON      ┌─────────────────────────────────────────────┐
│  Next.js UI  │  ───────────────▶   │                 FastAPI backend                │
│ (frontend/)  │                     │                                                │
└──────────────┘                     │  api/        routes (projects, models, rag,    │
                                      │              analytics)                        │
                                      │  orchestration/  LangGraph StateGraph +        │
                                      │              SQLite checkpointer + runner      │
                                      │  agents/     8 specialist agents               │
                                      │  router/     hybrid LLM router + fallback      │
                                      │  memory/ rag/  ChromaDB (local embeddings)     │
                                      │  skills/     procedural library, injected      │
                                      │  analytics/  usage & cost tracking             │
                                      │  db/         SQLAlchemy (SQLite/Postgres)      │
                                      └───────────┬───────────────────┬───────────────┘
                                                  │                   │
                                          ┌───────▼──────┐    ┌────────▼─────────┐
                                          │   Ollama     │    │  Cloud LLM APIs  │
                                          │ (local, free)│    │ Claude/GPT/Gemini│
                                          └──────────────┘    └──────────────────┘
```

## The pipeline (LangGraph)

The pipeline is a `StateGraph` with one node per phase, wired in order:

```
START → product_manager → system_design → backend_engineer → frontend_engineer
      → qa_engineer → security_engineer → devops_engineer → cost_estimation → END
```

It is compiled with:

- a **SQLite checkpointer** (`data/checkpoints.sqlite`), keyed by `thread_id = project_id`,
  so a run's position survives process restarts; and
- **`interrupt_after`** on every phase node, so each `invoke`/resume executes exactly one
  phase and then pauses — this is the mechanism behind the human-approval gates.

The **Backend** node additionally runs an **agent debate** first (System Design vs. Backend
vs. Security on the database/architecture choice); the verdict is injected into the Backend
agent's context and recorded as a `DebateRecord`.

### State

`PipelineState` (a `TypedDict`) carries the idea, routing config, accumulated
`prior_outputs` (phase → structured output), per-phase reviewer `feedback`, the
`last_result` (so the runner can persist it), and recorded `debates`.

## Request flow

| Action | What happens |
|---|---|
| `POST /api/projects` | Create a `Project` row (idea, routing mode, approval flag). |
| `POST /api/projects/{id}/run` | `runner.start()` → `graph.invoke(initial_state)` runs phase 1, pauses. The result is saved as a `PhaseResult` with status `pending_approval`; project → `awaiting_approval`. If approvals are off, the whole pipeline runs in a background task. |
| `GET /api/projects/{id}` | Returns the project with all phase outputs. |
| `POST /api/projects/{id}/approve` | Marks the latest phase approved, then `graph.invoke(None)` runs the next phase (or finishes). |
| `POST /api/projects/{id}/reject` | Re-runs the current phase with feedback, patches the checkpoint via `graph.update_state`, stays paused. |

When the graph reaches `END`, the project is marked `completed` and a short summary is written
to **long-term memory** for future runs to recall.

## The hybrid router

`router/router.py` resolves a `(provider, model)` **chain** for each request and tries each in
order, so a failing backend never stalls a run:

- **local_only** → only the local Ollama model.
- **manual** → the caller's `provider:model`, then the configured fallback chain.
- **auto** → a heuristic primary (strong cloud model for high-complexity work when a key is
  present; otherwise the free local model), then the fallback chain.

The **local Ollama model is always appended last** as the safety net. Every call's tokens,
cost, latency, and whether a fallback was used are recorded as a `UsageEvent`.

## The mockup: a build, not a document (`preview/`)

The Frontend phase's picture of the product is built in six passes, each sized from the
probed window of the model that answers it — no model name, window or token budget is
written down anywhere in `preview/`:

| pass | module | does |
|---|---|---|
| design | `design.py` | one call picks palette, type pairing, radius, shadow, density, voice → CSS tokens and a Tailwind config that also remaps `gray`/`blue` to the brand |
| plan | `plan.py` | routes, their sections and typed collections, from the PM features, FE pages and System Design data model; normalised so there is always >1 page, a list that filters/sorts and a form that stores |
| seed | `seed.py` | realistic records per collection, coerced to each field's type |
| sections | `sections.py` | one call per section, checked against its kind's `data-*` contract, repaired once, replaced by `templates.py` if still broken |
| runtime | `runtime.js` | the platform's router, store, forms, filters, sort, modals and toasts — inlined, never model-written |
| verify | `verify.py` | structural checks on the assembled site, plus a headless render when Playwright is installed |

A build runs on its own thread (`jobs.py`) and reports its pass and section count to
`GET /preview`; `POST /preview/generate` returns at once. Edits to a site are checked
against the same contract and refused (422) if they would break a section's bindings.

## The build: scaffold + compile gate (`build/`)

- `layout.py` places each agent's file: backend code under `backend/`, frontend under
  `frontend/`, tests by the side they test, infrastructure at the root.
- `scaffold.py` writes the boilerplate from the stack charter and the imports agents
  actually wrote — `package.json` (npm workspaces at the root), `tsconfig`/`jsconfig`,
  `next.config`, `tailwind.config`, `postcss.config`, `.env.example`, `requirements.txt`,
  a SQL migration runner and an initial migration from the data model — and fixes the
  mechanical Next.js failures (`"use client"`, `<Link><a>`, missing stylesheets).
- `check.py` compiles each code phase in the context of the build so far: Python's
  parser, TypeScript's for JS/TS/JSX (`check_js.cjs`, parser fetched by `toolchain.py`),
  and import resolution against the tree and the platform's package list. Problems join
  the agent's repair round; what survives is `PhaseResult.build_status/build_note`, and a
  build that still does not compile stops at a `build` gate instead of `ship`.

## Evaluation harness

`python -m scripts.eval_harness` runs fixed ideas unattended and scores each build with
`evals/scorecard.py`: schema conformance, charter violations, file and byte counts,
whether it compiles, the mockup's pages/sections/checks, and console errors. Results
land in `data/evals/<timestamp>.json`, so a prompt change is measured against the last run.

## Data model (SQLAlchemy)

- `Project` — the build and its pipeline status.
- `PhaseResult` — one agent's output + approval state (multiple rows per phase if re-run),
  and whether it matched its shape, the stack charter, and compiled.
- `PreviewRevision` — one version of the mockup; a built one carries its build `report`.
- `DebateRecord` — a recorded agent debate and verdict.
- `UsageEvent` — one LLM call's usage/cost (powers analytics).
- `KnowledgeDoc` — metadata for an uploaded RAG document (chunks live in ChromaDB).

`Project.skill_overrides` and `PhaseResult.skills_used` are the two columns the skill
library adds: what a build was told to force on or off, and what each phase was actually
given. The second is nullable rather than defaulted, because a row written before the
library existed cannot say it was offered skills and took none.

## Memory & RAG

Both use ChromaDB with **local embeddings via Ollama** (`nomic-embed-text`). They degrade to
no-ops if the vector store or embedding model is unavailable, so the pipeline never hard-fails
on them.

- **Memory** (`memory/store.py`) — a per-project summary written on completion, recalled into
  future agents' context.
- **RAG** (`rag/`) — uploaded docs are chunked, embedded, and queried for phase-relevant
  context.

## Skills (`skills/`)

The third thing an agent is given, beside those two, and the one that is not about this
project: procedure that holds across projects, kept as `SKILL.md` files in `backend/skills/`
(shipped) and `data/skills/` (yours, shadowing a shipped one of the same name).

- `loader.py` — one file: frontmatter, body, and the two rules checked as it loads (a length
  ceiling, and no instructions about output format, both measured on the block that is
  actually injected).
- `registry.py` — the library on disk, which skills are switched off, and how to change them.
- `selection.py` — which skills a phase gets: keyword score against the idea, the phase and
  what earlier phases wrote, decided **before** the model call so a phase costs no extra
  latency. Pinned skills sort first.

Delivery is prompt injection rather than provider tool-calling, because every provider takes
the same `list[ChatMessage]` and a 7B local model has no reliable tool loop. The packing is
in `agents/base.py`, not here: `skills` is a claimant in `_CONTEXT_SHARE` beside `rag` and
`memory`, so a skill's room comes out of the same allocator everything else is sized by — and
whatever it cannot spend (a procedure is injected whole or not at all) goes back to the
sections that can. Like memory and RAG, an empty or unreadable library is a no-op.
