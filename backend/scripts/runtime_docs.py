"""Write `docs/RUNTIMES.md` from the adapter table, so the doc never drifts from it.

    cd backend
    .venv/bin/python -m scripts.runtime_docs          # rewrite docs/RUNTIMES.md
    .venv/bin/python -m scripts.runtime_docs --check  # exit 1 if it is out of date

The table (`app/router/runtimes/table.py`), what the connector refuses
(`app/connector/protocol.py`) and the minimum-version table (`advisories.json`)
are the sources; this only lays them out. A test fails when the file is stale.
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.connector import protocol as P
from app.router.runtimes import hygiene, table

DOC = Path(__file__).resolve().parents[2] / "docs" / "RUNTIMES.md"

#: How detection tells each runtime apart — the one fingerprint, in words.
RECOGNISED_BY = {
    "ollama": "its root banner, `Ollama is running`",
    "lmstudio": "`/api/v0/models` entries with `type` and `state`",
    "llamacpp": "`owned_by: llamacpp` on `/v1/models`, `/props`",
    "vllm": "`owned_by: vllm` on `/v1/models`, `/version`",
    "sglang": "`owned_by: sglang` on `/v1/models`, `/server_info`",
    "koboldcpp": "`/api/extra/version` → `result: KoboldCpp`",
    "localai": "`/.well-known/localai.json`, or `/system` listing backends",
    "llamafile": "nothing of its own — it *is* a llama.cpp server, so it's read as llama.cpp unless you say `--runtime llamafile`",
    "mlx": "model entries without `owned_by`, `/health` ok, no `/props`",
    "jan": "`owned_by` of `llama.cpp`, `mlx` or `remote`",
    "tgw": "`/v1/internal/model/info` with `model_name` and `loader`",
    "gpt4all": "`owned_by: humanity` on `/v1/models`",
    "dmr": "its root banner, or `owned_by: docker` under `/engines/v1/models`",
    "foundry": "`/openai/status` with `Endpoints`",
    "openai-compatible": "`/v1/models` in the dialect's shape — used only once you add or confirm it",
}

#: Advisories deliberately left out of the table, and why.
NOT_LISTED = (
    "Ollama CVE-2024-39719 (sources disagree on the fix)",
    "Ollama CVE-2025-63389 (no patch — missing authentication on the model API; keep Ollama on loopback)",
    "LocalAI CVE-2024-6983 (fixed version disputed)",
    "SGLang CVE-2026-3060 (no fixed version published)",
    "text-generation-webui CVE-2026-35484, CVE-2026-35483, CVE-2026-35487 (fixed in 4.3) and CVE-2026-35050 "
    "(fixed in 4.1.1): its API reports no version, so they can't be checked — update it to 4.3 or later",
)


def _code(item: str) -> str:
    return f"`{item}`" if item.startswith(("/", "POST", "DELETE")) else item


def render() -> str:
    lines = [
        "# Local model runtimes",
        "",
        "Every runtime below works the same way in this app, in **direct mode** (the backend calls it) and "
        "through the **connector** (a paired computer calls it for the backend). None is preferred. This page "
        "is written from the adapter table in "
        "[`backend/app/router/runtimes/table.py`](../backend/app/router/runtimes/table.py) by "
        "`python -m scripts.runtime_docs`; each row links where it was checked (2026-09-27).",
        "",
        "## What each reports and takes",
        "",
        "| Runtime | Default port | Recognised by | Context window | Structured output | Thinking control "
        "| Embeddings | Listens on every interface by default | Source |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for spec in table.RUNTIMES:
        f = spec.facts
        assert f is not None, spec.id
        if spec.ports:
            port = ", ".join(str(p) for p in spec.ports)
        else:
            port = "any" if spec.id == table.GENERIC else "chosen at start — add it by address"
        lines.append(
            f"| {spec.label} | {port} | {RECOGNISED_BY[spec.id]} | {f.context} | `{f.structured}` | {f.thinking} "
            f"| {'yes' if f.embeddings else 'no'} | {'**yes**' if f.listens_everywhere else 'no'} "
            f"| [link]({f.source}) |"
        )
    lines += [
        "",
        "Structured output, strongest first: `schema` (decoding held to a JSON Schema), `grammar`, `json` (valid "
        "JSON, any shape), `none` (the shape is in the prompt). Whatever the level, every answer is validated and "
        "repaired, and a runtime that refuses a level is asked with the next one down — and not asked again. A "
        "window that isn't reported uses the configured fallback (`MODEL_CONTEXT_FALLBACK_TOKENS`), Settings says "
        "so on the model, and you set the real one per model under **Settings → Tune → Context window**.",
        "",
        "Ports are candidates, never identities: llama.cpp, llamafile, MLX-LM and LocalAI all default to 8080, and "
        "each is told apart by its answer. Something that answers on a default port but matches none of these — "
        "`python -m http.server 8000`, say — is listed as unknown and never used until you add it.",
        "",
        "## Status in this release",
        "",
        "Verified against running servers on this project's test machine: **Ollama** and **llama.cpp** "
        "(`llama-server`) — full builds in direct mode (#43, #44) and through the connector (#47). The others are "
        "built from each runtime's documentation and source (linked above), and their fingerprints and fields are "
        "covered by tests with payloads in each runtime's shape; the adapter reads every field defensively, so an "
        "answer that differs falls back to the generic OpenAI-compatible behaviour rather than failing. vLLM and "
        "SGLang need a supported GPU; MLX-LM needs Apple silicon; Foundry Local runs on Windows and macOS.",
        "",
        "## What the connector refuses",
        "",
        "The connector performs " + str(len(P.OPS)) + " typed operations — "
        + ", ".join(f"`{op}`" for op in P.OPS)
        + " — and refuses everything else, writing each refusal to `~/.aiteam-connect/connector.log`. It never "
        "calls these, whatever the server asks (`app/connector/protocol.py`, `REFUSED`):",
        "",
    ]
    for runtime, refused in P.REFUSED.items():
        name = "Every runtime" if runtime == "*" else table.spec_for(runtime).label
        lines.append(f"- **{name}**: " + ", ".join(_code(item) for item in refused))
    lines += [
        "",
        "## Runtime hygiene",
        "",
        "Settings, the Setup tab and the connector's terminal warn — never block — when a runtime:",
        "",
        "- **is older than a known security fix.** The minimum-version table is data: "
        "[`backend/app/router/runtimes/advisories.json`](../backend/app/router/runtimes/advisories.json), one entry "
        "per advisory with its first fixed version and a link. Add an entry there, or point "
        "`RUNTIME_ADVISORIES_FILE` at your own copy, to update it without a release. A version that can't be read "
        "is never taken for an old one; a pre-release of the fixed version (`0.17.1-rc0`) counts as older.",
        "- **is reachable from your network.** Checked by connecting to the runtime's port on the computer's own "
        "network address: a runtime bound to 127.0.0.1 refuses that, one bound to 0.0.0.0 accepts it. Nothing "
        "beyond the computer is contacted.",
        "",
        "| Runtime | Advisory | First fixed |",
        "|---|---|---|",
    ]
    for runtime, entries in hygiene.table().items():
        for entry in entries:
            lines.append(f"| {table.spec_for(runtime).label} | [{entry['id']}]({entry['url']}) | {entry['fixed']} |")
    lines += [
        "",
        "Not in the table (as of 2026-09-27): " + "; ".join(NOT_LISTED) + ".",
        "",
        "Never bind a runtime to `0.0.0.0` and never set `OLLAMA_ORIGINS=*` "
        "([CVE-2024-28224, DNS rebinding](https://www.nccgroup.com/research-blog/technical-advisory-ollama-dns-"
        "rebinding-attack-cve-2024-28224/)). The connector reaches the runtime on its own computer and dials out "
        "itself, so it never needs either.",
        "",
        "## The website never probes your computer",
        "",
        "The website doesn't fetch `http://127.0.0.1` from your browser; the connector is the only thing that looks, "
        "and it's the source of truth for what your computer runs. A browser probe would meet "
        "[Chrome's Local Network Access prompt](https://developer.chrome.com/blog/local-network-access) (enforced "
        "from Chrome 142) and Safari's mixed-content block on `http://localhost` from an https page "
        "([WebKit 171934](https://bugs.webkit.org/show_bug.cgi?id=171934)), and would only ever see model lists — "
        "so it isn't built. If it ever is, it may only `GET` model-list endpoints, and the connector's answer wins.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
        if current != text:
            print(f"{DOC} is out of date: run `python -m scripts.runtime_docs`.")
            return 1
        return 0
    DOC.write_text(text, encoding="utf-8")
    print(f"Wrote {DOC}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
