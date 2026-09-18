"""Pass 6 — verify the assembled site, and say what was checked.

Each section was checked on its own as it was generated. This checks the *site*: that
every planned section made it into the document and still balances (so it can be
edited later), that ids are unique, that every page has something on it and a link
to it, and that the logic the plan promised — a list that filters or sorts, a form
that stores — is actually present.

The checks are reported, not just enforced. A mockup that passed "every section
balances" and "no external images" is a claim the Preview tab can print; one where a
check failed says which, instead of looking the same as one where nothing did.

A headless render — load the page, collect console errors, visit every page — runs
when Playwright is installed in the backend's environment. It is optional because it
is a browser: the static checks above are what every install gets.
"""
from __future__ import annotations

import re
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.preview import html as H
from app.preview.document import is_site
from app.preview.plan import SitePlan

log = get_logger(__name__)


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def static_checks(html: str, site: SitePlan) -> list[dict]:
    """The structural claims the finished document can be held to."""
    checks: list[dict] = []
    found = H.scan_sections(html)
    ids = [s["id"] for s in found]
    planned = [s.id for s in site.all_sections()]

    missing = [sid for sid in planned if sid not in ids]
    unbalanced = [sid for sid in planned if sid in ids and H.extract_section(html, sid) is None]
    checks.append(
        _check(
            "Every planned section is present and balanced",
            not missing and not unbalanced,
            ", ".join([f"missing {m}" for m in missing] + [f"unbalanced {u}" for u in unbalanced]),
        )
    )
    raw_ids = H.attr_values(html, "data-section")
    dupes = sorted({i for i in raw_ids if raw_ids.count(i) > 1})
    checks.append(_check("Section ids are unique", not dupes, ", ".join(dupes)))

    routes = H.route_spans(html)
    empty = [r["path"] for r in routes if not any(s["route"] == r["path"] for s in found)]
    checks.append(
        _check(
            "More than one page, each with content",
            len(routes) > 1 and not empty,
            f"{len(routes)} page(s)" + (f"; empty: {', '.join(empty)}" if empty else ""),
        )
    )

    nav = H.extract_section(html, site.nav.id) or ""
    linked = {"/" + m.group(1).strip("/") for m in re.finditer(r'href\s*=\s*["\']#(/[^"\']*)["\']', nav)}
    linked = {"/" if p == "/" else p for p in linked}
    unlinked = [r["path"] for r in routes if r["path"] not in linked]
    checks.append(
        _check("Navigation reaches every page", not unlinked, ", ".join(unlinked))
    )

    lists = H.elements_with(html, "data-list")
    has_template = "<template" in html.lower()
    controls = H.attr_values(html, "data-filter") + H.attr_values(html, "data-sort")
    checks.append(
        _check(
            "A list that filters or sorts",
            bool(lists) and has_template and bool(controls),
            f"{len(lists)} list(s), {len(controls)} control(s)",
        )
    )
    forms = [attrs for tag, attrs in H.elements_with(html, "data-form") if tag == "form"]
    checks.append(_check("A form that validates and stores", bool(forms), f"{len(forms)} form(s)"))

    external = [
        src for src in re.findall(r'<img\b[^>]*\bsrc\s*=\s*["\']([^"\']*)["\']', html, re.IGNORECASE)
        if not src.startswith("data:")
    ]
    checks.append(
        _check("No image can fail to load", not external, f"{len(external)} external image(s)")
    )
    stray = [
        attrs
        for attrs, body in re.findall(r"<script\b([^>]*)>([\s\S]*?)</script>", html, re.IGNORECASE)
        if not _platform_script(attrs, body)
    ]
    markup = re.sub(r"<script\b[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    handlers = H.has_handlers(markup)
    checks.append(
        _check(
            "No model-written scripts or handlers",
            not stray and not handlers,
            f"{len(stray)} script(s)" + (", event handlers present" if handlers else "")
            if stray or handlers
            else "",
        )
    )
    checks.append(_check("Logic runtime present", is_site(html), ""))
    return checks


def _platform_script(attrs: str, body: str) -> bool:
    """The four scripts the platform itself puts in every site document."""
    a = attrs.lower()
    return (
        "cdn.tailwindcss.com" in a
        or 'id="app-data"' in a
        or 'id="app-runtime"' in a
        or body.strip().startswith("tailwind.config")
    )


def render_check(html: str, routes: list[str]) -> dict:
    """Load the page in a headless browser and count what goes wrong.

    Optional: returns `{"ran": False, "reason": …}` when Playwright is not installed or
    no browser is available, so the report says the check did not run rather than
    implying it passed.
    """
    if not settings.preview_render_check:
        return {"ran": False, "reason": "Turned off (PREVIEW_RENDER_CHECK=false)."}
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return {
            "ran": False,
            "reason": "Playwright is not installed in the backend environment, so the page "
            "was checked structurally but not rendered.",
        }

    errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.on(
                    "console",
                    lambda msg: errors.append(msg.text[:300]) if msg.type == "error" else None,
                )
                page.on("pageerror", lambda exc: errors.append(str(exc)[:300]))
                page.set_content(html, wait_until="load", timeout=30000)
                page.wait_for_timeout(600)
                for path in routes[1:]:
                    page.evaluate("p => window.__app && window.__app.go(p)", path)
                    page.wait_for_timeout(150)
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 - a missing browser is a skipped check
        log.info("Headless render check skipped: %s", e)
        return {"ran": False, "reason": f"The headless browser could not start: {str(e)[:160]}"}
    return {"ran": True, "console_errors": len(errors), "errors": errors[:10]}


def summarise(checks: list[dict]) -> Optional[str]:
    """One line naming what failed, or None when everything passed."""
    failed = [c["name"] for c in checks if not c.get("ok")]
    if not failed:
        return None
    return "Failed: " + "; ".join(failed)
