"""Node, and the TypeScript parser the compile gate reads JavaScript with.

The gate needs a real parser for JavaScript and TypeScript — JSX included, which
`node --check` rejects outright — and TypeScript's is the one every Next.js build
already trusts. It is not a dependency of this Python service, so the gate keeps its
own copy under `BUILD_TOOLCHAIN_DIR`, installed on first use when npm is available.

When there is no Node at all, or the install cannot happen, the gate says so: the
files are reported *unchecked*, never passed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

#: The parser version the gate reads with. A property of the checker, not of the
#: generated project — which pins its own TypeScript through the scaffold.
TYPESCRIPT = "typescript@5.5.4"

_lock = threading.Lock()
_state: dict[str, object] = {"tried": False, "typescript": None, "reason": None}


def node() -> Optional[str]:
    return shutil.which("node")


def _dir() -> str:
    return os.path.abspath(settings.build_toolchain_dir)


def _installed(base: str) -> Optional[str]:
    candidate = os.path.join(base, "node_modules", "typescript")
    return candidate if os.path.isfile(os.path.join(candidate, "package.json")) else None


def typescript() -> Optional[str]:
    """Path to a usable `typescript` package, installing it once if allowed."""
    with _lock:
        found = _installed(_dir())
        if found:
            _state.update(typescript=found, reason=None)
            return found
        if _state["tried"]:
            return _state["typescript"]  # type: ignore[return-value]
        _state["tried"] = True
        if node() is None:
            _state["reason"] = "Node.js is not installed, so JavaScript was not parsed."
            return None
        if not settings.build_check_provision:
            _state["reason"] = (
                f"No TypeScript parser in {_dir()} and BUILD_CHECK_PROVISION is off, so "
                "JavaScript was not parsed."
            )
            return None
        npm = shutil.which("npm")
        if npm is None:
            _state["reason"] = "npm is not installed, so the JavaScript parser could not be fetched."
            return None
        base = _dir()
        try:
            os.makedirs(base, exist_ok=True)
            manifest = os.path.join(base, "package.json")
            if not os.path.exists(manifest):
                with open(manifest, "w", encoding="utf-8") as fh:
                    json.dump({"name": "build-check-toolchain", "private": True}, fh)
            log.info("Installing %s for the compile gate into %s …", TYPESCRIPT, base)
            subprocess.run(
                [npm, "install", "--no-audit", "--no-fund", "--loglevel=error", TYPESCRIPT],
                cwd=base,
                check=True,
                capture_output=True,
                timeout=300,
            )
        except (OSError, subprocess.SubprocessError) as e:
            detail = getattr(e, "stderr", b"") or b""
            _state["reason"] = (
                "The JavaScript parser could not be installed "
                f"({(detail.decode(errors='replace') or str(e)).strip()[:200]}), so JavaScript was not parsed."
            )
            log.warning("Compile gate toolchain install failed: %s", _state["reason"])
            return None
        found = _installed(base)
        _state["typescript"] = found
        if found is None:
            _state["reason"] = "npm finished but TypeScript is not where it should be."
        return found


def unavailable_reason() -> Optional[str]:
    return _state.get("reason")  # type: ignore[return-value]


def warm_up() -> None:
    """Fetch the parser in the background at startup, so no phase waits on npm."""
    if not settings.enforce_build_check:
        return
    threading.Thread(target=typescript, name="build-toolchain", daemon=True).start()
