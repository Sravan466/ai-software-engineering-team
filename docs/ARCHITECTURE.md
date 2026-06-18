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

## Data model (SQLAlchemy)

- `Project` — the build and its pipeline status.
- `PhaseResult` — one agent's output + approval state (multiple rows per phase if re-run).
- `DebateRecord` — a recorded agent debate and verdict.
- `UsageEvent` — one LLM call's usage/cost (powers analytics).
- `KnowledgeDoc` — metadata for an uploaded RAG document (chunks live in ChromaDB).

## Memory & RAG

Both use ChromaDB with **local embeddings via Ollama** (`nomic-embed-text`). They degrade to
no-ops if the vector store or embedding model is unavailable, so the pipeline never hard-fails
on them.

- **Memory** (`memory/store.py`) — a per-project summary written on completion, recalled into
  future agents' context.
- **RAG** (`rag/`) — uploaded docs are chunked, embedded, and queried for phase-relevant
  context.
