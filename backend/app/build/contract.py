"""What the code-writing agents are told about the build they are writing into.

Three facts, each of which used to be left for a model to guess and which it guessed
differently in every phase: where files go, which files are not theirs to write, and
which packages exist. Printed into the system prompt — beside the stack charter,
for the same reason: it is an instruction that has to survive a small window, not
context that can be trimmed to fit.
"""
from __future__ import annotations

from typing import Optional

from app.build import packages as pkg
from app.core.constants import Phase

_OWNED = (
    "package.json, lockfiles, tsconfig.json/jsconfig.json, next.config.*, tailwind.config.*, "
    "postcss.config.*, vite.config.*, requirements.txt, .env.example, migrate.py"
)


def prompt_block(phase_key: str, charter=None) -> Optional[str]:
    """The platform contract for one phase, or None for a phase that writes no code."""
    language = charter.get("language").token if charter is not None and charter.get("language") else None
    python_backend = language in (None, "python")

    if phase_key == Phase.BACKEND_ENGINEER.value:
        where = "Write every file under backend/ (e.g. backend/main.py, backend/app/models.py)."
        if python_backend:
            available = "Python packages you may import: " + pkg.pip_vocabulary(include_tests=False)
        else:
            available = "npm packages you may import: " + pkg.npm_vocabulary(include_server=True, include_tests=False)
        extra = (
            "- Put database schema changes in backend/migrations/NNNN_name.sql; the platform "
            "writes the runner that applies them.\n"
        )
    elif phase_key == Phase.FRONTEND_ENGINEER.value:
        where = (
            "Write every file under frontend/ (e.g. frontend/pages/index.jsx or "
            "frontend/app/page.tsx, frontend/components/Header.jsx)."
        )
        available = "npm packages you may import: " + pkg.npm_vocabulary(include_server=False, include_tests=False)
        extra = (
            "- Import every component and hook you use, from its file. A component used "
            "without an import fails the build.\n"
            "- With Next.js, <Link href=\"/x\">Text</Link> wraps its text directly — no <a> "
            "inside it.\n"
        )
    elif phase_key == Phase.QA_ENGINEER.value:
        where = (
            "Put backend tests under backend/tests/ and frontend tests under "
            "frontend/__tests__/, importing the code they test by its real path."
        )
        available = (
            "Packages you may import — Python: "
            + pkg.pip_vocabulary(include_tests=True)
            + ". npm: "
            + pkg.npm_vocabulary(include_server=not python_backend, include_tests=True)
        )
        extra = "- Import every testing helper you use (render, screen, fireEvent, …).\n"
    else:
        return None

    return (
        "PLATFORM SCAFFOLD — the platform writes the project's boilerplate from the stack "
        "charter; you write only this application's own files.\n"
        f"- {where}\n"
        f"- Do NOT write {_OWNED}. They are generated from what you import, and a copy "
        "of yours would be replaced.\n"
        f"{extra}"
        f"- {available}. Anything else must be a file in this build.\n"
        "- Every relative import must point at a file you or an earlier phase wrote.\n"
        "- Write each file's code with real newlines. The build is compiled after you "
        "answer: a file that does not parse, uses a name it never imports, or imports "
        "something that does not exist is sent back to you with the error."
    )
