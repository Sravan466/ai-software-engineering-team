"""Visual-preview tests: pure HTML surgery + the generate/edit/undo routes."""
from __future__ import annotations

from app.preview.html import extract_section, replace_section, scan_sections
from app.router.router import router as model_router
from app.schemas.llm import LLMResponse, Usage

SAMPLE = (
    "<!doctype html><html><head>"
    '<script src="https://cdn.tailwindcss.com"></script></head><body>'
    '<section data-section="hero" data-label="Hero Banner">'
    '<div class="p-4"><h1>Find homes</h1></div></section>'
    '<section data-section="features" data-label="Features">'
    "<ul><li>A</li><li>B</li></ul></section>"
    '<footer data-section="footer" data-label="Footer"><p>(c) 2026</p></footer>'
    "</body></html>"
)


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_scan_sections_orders_and_labels():
    sections = scan_sections(SAMPLE)
    assert [s["id"] for s in sections] == ["hero", "features", "footer"]
    assert sections[0]["label"] == "Hero Banner"
    # Falls back to a title-cased id when data-label is absent.
    assert scan_sections('<section data-section="cta-row">x</section>')[0]["label"] == "Cta Row"


def test_extract_section_returns_balanced_element():
    hero = extract_section(SAMPLE, "hero")
    assert hero is not None
    assert hero.startswith('<section data-section="hero"')
    assert hero.endswith("</section>")
    assert "<h1>Find homes</h1>" in hero
    # A non-<section> tag (footer) is handled by its own tag name, not a hardcoded one.
    assert extract_section(SAMPLE, "footer").startswith("<footer")
    assert extract_section(SAMPLE, "nope") is None


def test_replace_section_only_touches_target():
    new = replace_section(SAMPLE, "features", '<section data-section="features">NEW</section>')
    assert "NEW" in new
    assert "<li>A</li>" not in new  # the features block was swapped out
    assert "Find homes" in new  # hero untouched
    assert "(c) 2026" in new  # footer untouched
    # Unknown id is a no-op.
    assert replace_section(SAMPLE, "ghost", "X") == SAMPLE


# ── routes ───────────────────────────────────────────────────────────────────
def _fake_preview(messages, **kwargs) -> LLMResponse:
    """Return a full document for generate, a single fragment for edit."""
    is_edit = "front-end editor" in messages[0].content
    text = (
        '<section data-section="hero" data-label="Hero Banner"><h1>EDITED HERO</h1></section>'
        if is_edit
        else SAMPLE
    )
    return LLMResponse(
        text=text,
        provider="mock",
        model="mock-model",
        usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
    )


def _create(client) -> str:
    r = client.post("/api/projects", json={"idea": "A real estate landing site", "routing_mode": "local_only"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_generate_then_edit_then_undo(client, monkeypatch):
    monkeypatch.setattr(model_router, "complete", _fake_preview)
    pid = _create(client)

    # Nothing yet.
    empty = client.get(f"/api/projects/{pid}/preview").json()
    assert empty["html"] is None and empty["revisions"] == []

    # Generate -> one revision, sections detected.
    gen = client.post(f"/api/projects/{pid}/preview/generate").json()
    assert gen["html"].startswith("<!doctype")
    assert [s["id"] for s in gen["sections"]] == ["hero", "features", "footer"]
    assert len(gen["revisions"]) == 1 and gen["revisions"][0]["source"] == "generated"

    # Edit the hero -> new revision; only the hero changed.
    edited = client.post(
        f"/api/projects/{pid}/preview/edit",
        json={"section_id": "hero", "instruction": "make the headline say EDITED HERO"},
    ).json()
    assert "EDITED HERO" in edited["html"]
    assert "<li>A</li>" in edited["html"]  # features survived
    assert len(edited["revisions"]) == 2 and edited["revisions"][0]["source"] == "edited"

    # Preview tokens are folded into the project's analytics.
    assert client.get(f"/api/analytics/projects/{pid}").json()["calls"] >= 2

    # Undo -> back to the generated revision.
    undone = client.post(f"/api/projects/{pid}/preview/undo").json()
    assert "EDITED HERO" not in undone["html"]
    assert len(undone["revisions"]) == 1


def test_edit_requires_existing_preview(client, monkeypatch):
    monkeypatch.setattr(model_router, "complete", _fake_preview)
    pid = _create(client)
    r = client.post(
        f"/api/projects/{pid}/preview/edit",
        json={"section_id": "hero", "instruction": "x"},
    )
    assert r.status_code == 400 and "generate one first" in r.text


def test_edit_unknown_section_400(client, monkeypatch):
    monkeypatch.setattr(model_router, "complete", _fake_preview)
    pid = _create(client)
    client.post(f"/api/projects/{pid}/preview/generate")
    r = client.post(
        f"/api/projects/{pid}/preview/edit",
        json={"section_id": "ghost", "instruction": "x"},
    )
    assert r.status_code == 400 and "isn't in the current preview" in r.text
