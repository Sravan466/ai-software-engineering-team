# Roadmap

The backbone is in place and runs end-to-end. Tracking what's done and what's next.

## Done (v0.1 — current)

- [x] Project scaffold (backend + frontend + docs + Docker)
- [x] Hybrid LLM router: Ollama (default) + Claude + GPT + Gemini, with Auto / Manual /
      Local-Only modes and a fallback chain
- [x] All 8 specialist agents with structured-output prompts
- [x] LangGraph `StateGraph` pipeline with SQLite checkpointing + human-approval interrupts
- [x] Agent debate step before the Backend phase
- [x] FastAPI: projects, pipeline control (run/approve/reject), models, RAG, analytics
- [x] SQLite persistence (Postgres-ready)
- [x] Long-term memory + RAG knowledge base (ChromaDB, local embeddings)
- [x] Usage & cost analytics
- [x] Minimal Next.js UI: create, progress tracker, per-phase review, analytics
- [x] End-to-end pipeline tests with a stubbed LLM (approval loop, reject/regenerate,
      debate, auto-run) — runnable with `pytest`, no Ollama required

## Next (v0.2)

- [ ] Stream phase generation token-by-token to the UI (WebSocket / SSE)
- [ ] Render the architecture/ER/deployment Mermaid diagrams in the UI
- [ ] Export deliverables (zip of generated code + docs)
- [ ] Richer Auto-routing (use the model registry's complexity/cost signals per phase)
- [ ] More tests: router fallback chain, RAG ingest/query, provider adapters
- [ ] Auth (JWT/OAuth) + multi-user project isolation
- [ ] Per-phase model overrides (e.g. local for codegen, cloud for security review)

## Later

- [ ] Parallel agents where the pipeline allows (frontend ∥ tests)
- [ ] Pluggable orchestrators (CrewAI / AutoGen) behind the same runner interface
- [ ] Evaluation harness scoring generated artifacts
