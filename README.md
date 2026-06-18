# AI Software Engineering Team

> A multi-agent AI platform that simulates a complete software engineering organization — turning a single product idea into production-ready software.

This repository contains the implementation of the platform described in
[`ai-software-engineering-team.md`](./ai-software-engineering-team.md).

Multiple specialized AI agents (Product Manager, System Design, Backend, Frontend, QA,
Security, DevOps, Cost) collaborate through a **LangGraph** pipeline with **human approval
gates**. It runs on **local models via Ollama** (default, zero cost) or **cloud LLMs**
(Claude, GPT, Gemini) through a hybrid router with automatic fallback.

---

## Architecture at a glance

```mermaid
flowchart LR
    subgraph Client["🖥️  Frontend · Next.js 14"]
        UI["Dashboard /<br/>Landing /landing<br/>Settings /settings"]
    end

    subgraph API["⚙️  Backend · FastAPI"]
        REST["REST API<br/>/api/projects"]
        ORCH["Orchestration<br/>LangGraph StateGraph<br/>+ approval gates"]
        ROUTER["Hybrid LLM Router<br/>+ automatic fallback"]
        MEM["Memory + RAG<br/>(ChromaDB)"]
        ANALYTICS["Analytics<br/>tokens · cost"]
        DB[("SQLite / Postgres<br/>+ SQLite checkpointer")]
    end

    subgraph Models["🧠  LLM Providers"]
        OLLAMA[["Ollama<br/>qwen2.5:7b<br/>(default · local · free)"]]
        CLOUD[["Claude · GPT · Gemini<br/>(optional · cloud)"]]
    end

    UI <-->|HTTP / JSON| REST
    REST --> ORCH
    ORCH --> ROUTER
    ORCH <--> MEM
    ORCH --> ANALYTICS
    ORCH <--> DB
    ROUTER -->|default| OLLAMA
    ROUTER -.->|fallback / manual| CLOUD
```

```
frontend/   Next.js 14 + Tailwind + ShadCN-style UI
backend/    FastAPI service
  app/
    router/         Hybrid LLM routing & fallback (Ollama / Claude / GPT / Gemini)
    agents/         The 8 specialist agents
    orchestration/  LangGraph StateGraph + human-in-the-loop approvals
    memory/         Long-term project memory (ChromaDB)
    rag/            Knowledge base & retrieval (ChromaDB)
    analytics/      Token usage & cost tracking
    api/            REST endpoints
    db/             SQLAlchemy models (SQLite default, Postgres optional)
docs/       Architecture & agent documentation
```

The component folders map 1:1 to the spec's project structure; in this implementation the
Python packages live under `backend/app/` so they share one import root and one process.

---

## Quick start (local, zero cost)

### 1. Install Ollama and pull a model

```bash
# https://ollama.com/download
ollama pull qwen2.5:7b      # default model used by the router
ollama serve                # usually already running as a service
```

### 2. Backend

Requires **Python 3.9+** (Docker uses 3.12).

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env       # tweak if you like; defaults work offline
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/docs for the interactive API.

Run the test suite (no Ollama needed — the LLM is stubbed):

```bash
pip install pytest
pytest -q
```

The tests exercise the full pipeline end-to-end: an 8-phase run through the
human-approval gates, the agent debate, reject/regenerate, auto-run mode, and analytics.

### 3. Frontend

```bash
cd frontend
npm install
npm run dev                   # http://localhost:3000
```

- **`/`** — the working dashboard (create a project, watch the live pipeline, approve phases).
- **`/landing`** — the marketing landing (Direction A · Blueprint): a full-bleed, self-contained
  React port of the Claude Design board, in `components/blueprint/` (state machine in `pipeline.ts`,
  styling in `blueprint.css`). The original Claude Design export is preserved under
  `design/claude-design-export/`.
- **`/settings`** — manage models at runtime (self-host): check / **download** the local Ollama
  model (live pull progress), and add your own **cloud API keys** (Claude / GPT / Gemini) without
  editing `.env`. Keys are stored on the backend only, in the gitignored
  `backend/data/providers.local.json`, and applied to the router immediately.

### Or with Docker

```bash
cp .env.example .env
docker compose up --build
```

---

## Using cloud models instead of / alongside Ollama

Set any of these in `.env` and the router will use them automatically in **Auto** mode,
or you can pick them explicitly in **Manual** mode:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
```

If a cloud call fails (quota, network), the router falls back down a configurable chain,
ending at the local Ollama model so the pipeline keeps running.

---

## Workflow

A product idea flows through **8 specialist agents** in order. After each phase the graph
**pauses for human approval** — you can approve to advance, or reject with feedback to
regenerate that phase. Every agent call goes through the hybrid router, so a phase runs on
local Ollama by default and falls back to a cloud model only if configured/needed.

```mermaid
flowchart TD
    Idea(["💡 Product idea"]) --> Create["POST /api/projects<br/>create project + LangGraph thread"]
    Create --> Run["POST /api/projects/:id/run<br/>start pipeline"]
    Run --> Agent["Run current phase agent"]

    Agent --> Router{{"Hybrid LLM Router"}}
    Router -->|default| Ollama[("Ollama · qwen2.5:7b")]
    Router -.->|fallback / manual| Cloud[("Claude · GPT · Gemini")]
    Ollama --> Output["Phase deliverable<br/>(stored + token/cost logged)"]
    Cloud --> Output

    Output --> Pause["⏸️ Pause for human approval<br/>(LangGraph interrupt)"]
    Pause --> Decision{"Approve or Reject?"}
    Decision -->|"Reject + feedback"| Agent
    Decision -->|Approve| More{"More phases left?"}
    More -->|Yes| Agent
    More -->|"No — final phase approved"| Done(["✅ Project complete<br/>deliverables stored per phase"])
```

### The 8 phases

```mermaid
flowchart LR
    PM["1 · Product<br/>Manager"] --> SD["2 · System<br/>Design"]
    SD --> BE["3 · Backend<br/>Engineer"]
    BE --> FE["4 · Frontend<br/>Engineer"]
    FE --> QA["5 · QA<br/>Engineer"]
    QA --> SEC["6 · Security<br/>Engineer"]
    SEC --> DO["7 · DevOps<br/>Engineer"]
    DO --> COST["8 · Cost<br/>Estimation"]
```

Each arrow is an approval gate. Agents also **debate** where their concerns overlap (e.g.
Security vs. Backend), and decisions are persisted to long-term memory for reuse.

### API in short

| Step | Call | What happens |
|------|------|--------------|
| 1 | `POST /api/projects` | Create a project + LangGraph thread from an idea |
| 2 | `POST /api/projects/{id}/run` | Start the pipeline (runs the next phase, then pauses) |
| 3 | `GET /api/projects/{id}` | Read the latest phase output |
| 4 | `POST /api/projects/{id}/approve` | Advance the graph to the next phase |
| 5 | `POST /api/projects/{id}/reject` | Send feedback → regenerate the current phase |

> Completion is **explicit**: the final phase must be approved (not merely reached) for the
> project to move to `completed`.

See [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) for the full flow.

---

## Status

This is an iterative build. The backbone (router + Ollama, all 8 agents, LangGraph
orchestration with approvals, FastAPI, SQLite, memory/RAG/analytics, minimal UI) is in
place and runs end-to-end. See [docs/ROADMAP.md](./docs/ROADMAP.md) for what's next.
