"""Visual-preview tests: HTML surgery, the site build's guarantees, and the routes."""
from __future__ import annotations

import json

from app.db.base import SessionLocal
from app.db.models import PreviewRevision
from app.preview import plan as P
from app.preview import seed as S
from app.preview.brief import SiteBrief
from app.preview.document import is_site, site_data
from app.preview.html import (
    clean_fragment,
    extract_section,
    replace_section,
    route_spans,
    scan_sections,
)
from app.preview.jobs import jobs
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
    # A single-page document has no routes, so no section is on one.
    assert all(s["route"] is None for s in sections)
    # Falls back to a title-cased id when data-label is absent.
    assert scan_sections('<section data-section="cta-row">x</section>')[0]["label"] == "Cta Row"


def test_sections_know_their_page():
    doc = (
        '<header data-section="nav">n</header>'
        '<div data-route="/" data-route-title="Home"><section data-section="a">x</section></div>'
        '<div data-route="/tasks" data-route-title="Tasks"><section data-section="b">y</section></div>'
    )
    assert [(r["path"], r["title"]) for r in route_spans(doc)] == [("/", "Home"), ("/tasks", "Tasks")]
    assert {s["id"]: s["route"] for s in scan_sections(doc)} == {"nav": None, "a": "/", "b": "/tasks"}


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


def test_clean_fragment_removes_what_could_run_or_escape():
    dirty = (
        "Sure! Here it is:\n```html\n"
        '<section data-section="x"><script>alert(1)</script>'
        '<a href="javascript:alert(1)" onclick="go()">Go</a>'
        '<form action="/submit" method="post"><button>Send</button></form>'
        "<style>body{display:none}</style></section>\n```"
    )
    clean = clean_fragment(dirty)
    assert clean.startswith('<section data-section="x">')
    for gone in ("<script", "onclick", "javascript:", "action=", "method=", "<style"):
        assert gone not in clean


def test_clean_fragment_drops_network_pictures_but_keeps_what_draws_locally():
    clean = clean_fragment(
        '<div style="background:url(https://x/y.jpg)"><picture><source srcset="a.webp">'
        '<img src="a.jpg" srcset="a.jpg 2x" alt="Hero"></picture><video src="v.mp4"></video>'
        '<svg><defs><linearGradient id="g"/></defs><rect fill="url(#g)"/></svg></div>'
    )
    assert "https://x" not in clean and "<video" not in clean and "<source" not in clean
    assert "srcset" not in clean and "<picture" not in clean
    assert '<img src="a.jpg" alt="Hero">' in clean  # the platform redraws this one
    assert 'fill="url(#g)"' in clean  # an in-document reference, not a request


# ── the plan's guarantees ────────────────────────────────────────────────────
def test_any_plan_is_normalised_into_a_site_with_logic():
    """One page, no list, no form, a dynamic route: still a site that works."""
    brief = SiteBrief(idea="Track reading habits", product="Shelf")
    spec = {
        "product_name": "Shelf",
        "collections": [
            {"name": "book", "label": "Books", "fields": [
                {"name": "title", "type": "string"},
                {"name": "price", "type": "int"},
                {"name": "status", "type": "text"},
                {"name": "password", "type": "string"},
            ]},
        ],
        "routes": [
            {"path": "/books/:id", "title": "Book", "purpose": "", "sections": [
                {"id": "Hero!", "kind": "banner", "label": "Top", "brief": ""},
            ]},
        ],
    }
    site = P.normalise(spec, brief, max_routes=4, max_sections=4)
    assert site.routes[0].path == "/"
    assert len(site.routes) >= 2
    kinds = [s.kind for r in site.routes for s in r.sections]
    assert "form" in kinds and ("list" in kinds or "table" in kinds)
    books = site.collection("books")
    assert books is not None
    types = {f.name: f.type for f in books.fields}
    # Money is money, a status is a choice, and a password is never a column.
    assert types["price"] == "currency" and types["status"] == "select"
    assert "password" not in types
    assert "/books" in [r.path for r in site.routes]  # the :id segment collapsed
    ids = [s.id for s in site.all_sections()]
    assert len(ids) == len(set(ids))


def test_seed_rows_are_coerced_to_their_field_types():
    site = P.normalise(P.fallback_spec(SiteBrief(idea="x", product="X"), 3),
                       SiteBrief(idea="x", product="X"), max_routes=3, max_sections=3)
    c = site.collections[0]
    select = next(f for f in c.fields if f.type == "select")
    raw = {c.name: [{select.name: select.options[0].upper(), "created_at": "Sep 3, 2026"}]}
    rows, sources = S.seed_rows(site, raw, 4)
    assert len(rows[c.name]) == 4
    assert rows[c.name][0][select.name] == select.options[0]  # matched case-insensitively
    assert rows[c.name][0]["created_at"] == "2026-09-03"
    assert sources[c.name] in ("mixed", "model")


# ── routes ───────────────────────────────────────────────────────────────────
def _fake(messages, **kwargs) -> LLMResponse:
    """Unusable JSON and prose for the build (so every pass falls back), and a
    well-formed hero for an edit — the build's floor, and the edit loop, in one stub."""
    system = messages[0].content
    options = kwargs.get("options")
    if "front-end editor" in system:
        text = (
            '<section data-section="home-hero" class="bg-surface"><div class="container-page">'
            '<h1 class="font-display text-5xl">EDITED HERO headline here</h1>'
            '<a class="btn btn-primary" href="#/">Start now</a></div></section>'
        )
    elif getattr(options, "json_schema", None):
        text = "{}"
    else:
        text = "I cannot help with that."
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


def _generate(client, pid: str) -> dict:
    started = client.post(f"/api/projects/{pid}/preview/generate")
    assert started.status_code == 202, started.text
    assert jobs.wait(pid, timeout=30)
    return client.get(f"/api/projects/{pid}/preview").json()


def test_generate_builds_a_site_then_edit_then_undo(client, monkeypatch):
    monkeypatch.setattr(model_router, "complete", _fake)
    pid = _create(client)

    empty = client.get(f"/api/projects/{pid}/preview").json()
    assert empty["html"] is None and empty["revisions"] == [] and empty["job"] is None

    gen = _generate(client, pid)
    assert is_site(gen["html"])
    assert len(gen["routes"]) >= 2
    assert gen["job"] is None  # a build that succeeded reports nothing further
    report = gen["report"]
    # Every pass fell back and every check still holds: the floor is a working site.
    assert report["passes"] == {"design": "fallback", "plan": "fallback", "seed": "generated"}
    assert all(c["ok"] for c in report["checks"]), report["checks"]
    assert report["counts"]["fallback"] == len(report["sections"])
    assert len(gen["revisions"]) == 1 and gen["revisions"][0]["source"] == "generated"
    assert {s["route"] for s in gen["sections"]} >= {"/", None}

    edited = client.post(
        f"/api/projects/{pid}/preview/edit",
        json={"section_id": "home-hero", "instruction": "make the headline say EDITED HERO"},
    ).json()
    assert "EDITED HERO" in edited["html"]
    assert is_site(edited["html"])
    hero = extract_section(edited["html"], "home-hero")
    # The platform owns the section's identity, whatever the model wrote on it.
    assert 'data-kind="hero"' in hero and 'data-label=' in hero
    assert len(edited["revisions"]) == 2 and edited["revisions"][0]["source"] == "edited"
    assert edited["report"] == report  # an edit carries the build it edited

    # Preview tokens are folded into the project's analytics — one event per call.
    assert client.get(f"/api/analytics/projects/{pid}").json()["calls"] >= report["calls"] + 1

    undone = client.post(f"/api/projects/{pid}/preview/undo").json()
    assert "EDITED HERO" not in undone["html"]
    assert len(undone["revisions"]) == 1


def test_an_edit_that_breaks_a_list_is_refused(client, monkeypatch):
    monkeypatch.setattr(model_router, "complete", _fake)
    pid = _create(client)
    gen = _generate(client, pid)
    data = site_data(gen["html"])
    listed = next(s["id"] for s in data["sections"] if s["kind"] == "list")

    # The stub answers every edit with a hero: no data-list, no template, no controls.
    r = client.post(
        f"/api/projects/{pid}/preview/edit",
        json={"section_id": listed, "instruction": "make it prettier"},
    )
    assert r.status_code == 422 and "wasn't applied" in r.text
    assert len(client.get(f"/api/projects/{pid}/preview").json()["revisions"]) == 1


def test_a_mockup_from_before_sites_is_still_editable(client, monkeypatch):
    monkeypatch.setattr(model_router, "complete", _fake)
    pid = _create(client)
    db = SessionLocal()
    db.add(PreviewRevision(project_id=pid, html=SAMPLE.replace('"hero"', '"home-hero"'), source="generated"))
    db.commit()
    db.close()

    edited = client.post(
        f"/api/projects/{pid}/preview/edit",
        json={"section_id": "home-hero", "instruction": "x"},
    ).json()
    assert "EDITED HERO" in edited["html"] and "<li>A</li>" in edited["html"]
    assert edited["report"] is None and edited["routes"] == []


def test_edit_requires_existing_preview(client, monkeypatch):
    monkeypatch.setattr(model_router, "complete", _fake)
    pid = _create(client)
    r = client.post(
        f"/api/projects/{pid}/preview/edit",
        json={"section_id": "hero", "instruction": "x"},
    )
    assert r.status_code == 400 and "generate one first" in r.text


def test_edit_unknown_section_400(client, monkeypatch):
    monkeypatch.setattr(model_router, "complete", _fake)
    pid = _create(client)
    _generate(client, pid)
    r = client.post(
        f"/api/projects/{pid}/preview/edit",
        json={"section_id": "ghost", "instruction": "x"},
    )
    assert r.status_code == 400 and "isn't in the current preview" in r.text


def test_the_document_carries_what_edits_are_checked_against(client, monkeypatch):
    monkeypatch.setattr(model_router, "complete", _fake)
    pid = _create(client)
    html = _generate(client, pid)["html"]
    data = site_data(html)
    assert data["version"] == 2
    assert set(data) >= {"routes", "collections", "sections", "design"}
    assert all(rows for rows in (c["rows"] for c in data["collections"].values()))
    # The JSON block cannot close the <script> it lives in, whatever the records say.
    block = html.split('id="app-data">', 1)[1].split("</script>", 1)[0]
    assert "</" not in block and json.loads(block.replace("<\\/", "</")) == data


def test_the_sites_data_survives_any_text_in_its_records():
    from app.preview.document import _json_for_script

    data = {"rows": [{"note": "<!-- not a comment -->", "html": "</script><b>"}]}
    encoded = _json_for_script(data)
    assert "<" not in encoded
    assert json.loads(encoded) == data


def test_an_outer_element_that_is_the_binding_is_not_unwrapped():
    from app.preview.plan import SectionPlan
    from app.preview.sections import unwrap

    section = SectionPlan(id="items", kind="list", label="Items", brief="", collection="items")
    inner, classes = unwrap('<div data-list="items" class="grid"><template><p data-field="name"></p></template></div>', section)
    assert inner.startswith('<div data-list="items"') and classes == ""
    inner, classes = unwrap('<section class="py-10 bg-surface"><h2>Items</h2></section>', section)
    assert inner == "<h2>Items</h2>" and classes == "bg-surface"


def test_handlers_are_stripped_however_they_are_attached():
    from app.preview.html import has_handlers

    for dirty in ('<svg/onload=alert(1)>', '<a href="#"onclick="x()">x</a>', "<b onmouseover='y()'>b</b>"):
        assert has_handlers(dirty)
        clean = clean_fragment(f"<div>{dirty}</div>")
        assert "alert" not in clean and "x()" not in clean and "y()" not in clean, clean
        assert not has_handlers(clean)


def test_text_that_looks_like_an_attribute_is_left_alone():
    from app.preview.html import has_handlers

    clean = clean_fragment('<p>Use code "ONSALE=20" at checkout</p><a href="#/">Go</a>')
    assert '"ONSALE=20" at checkout</p>' in clean and not has_handlers(clean)


def test_the_models_own_section_is_the_wrapper_and_its_binding_survives():
    from app.preview.plan import SectionPlan
    from app.preview.sections import unwrap, wrap

    section = SectionPlan(id="signup", kind="form", label="Sign up", brief="", collection="leads")
    inner, classes = unwrap(
        '<section data-section="signup" data-form="leads" data-success="Thanks!" '
        'class="py-20 bg-surface"><input name="email"><button type="submit">Go</button></section>',
        section,
    )
    # The binding lands on a <form> — the only element the runtime binds — with what
    # belongs to it, and the section id stays on the platform's wrapper alone.
    assert classes == "bg-surface" and inner.startswith('<form data-form="leads" data-success="Thanks!">')
    html = wrap(section, inner, classes)
    assert html.count('data-section="signup"') == 1 and 'data-form="leads"' in html


def test_the_header_keeps_its_navigation_landmark():
    from app.preview.plan import SectionPlan
    from app.preview.sections import unwrap, wrap

    section = SectionPlan(id="site-nav", kind="nav", label="Navigation", brief="")
    inner, classes = unwrap('<nav data-section="site-nav" class="flex gap-4"><a href="#/">Home</a></nav>', section)
    html = wrap(section, inner, classes)
    assert '<nav class="flex gap-4">' in html and html.count('data-section="site-nav"') == 1


def test_copy_that_mentions_url_is_left_alone():
    clean = clean_fragment('<p style="background:url(https://x/y.png)">Paste the url(s) of your feeds.</p>')
    assert "Paste the url(s) of your feeds." in clean and "https://x" not in clean


def test_copy_that_reads_like_an_attribute_keeps_its_words():
    clean = clean_fragment('<p>Choose from three sizes = small, medium or large.</p><img src="a.jpg" srcset="a.jpg 2x" alt="A">')
    assert "three sizes = small, medium or large." in clean and "srcset" not in clean
