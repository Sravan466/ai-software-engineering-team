"""A Render Blueprint (`render.yaml`) for a finished build, and what kind of app it is.

Render deploys from a git repository, never from uploaded files, and it has no
third-party sign-in. So a full-stack build goes to the user's GitHub with a
`render.yaml` at its root, and the user opens `render.com/deploy?repo=…`: Render
signs them in and creates every service in the Blueprint in *their* account. We never
hold a Render key.

Everything here is decided from what the platform already knows — the scaffold's
detected frameworks and the charter's database — never from a model.

The Blueprint carries variable **names** only. A value the user saved for their
database (#54) is listed with `sync: false`, which makes Render ask for it on its
own page; a secret the app merely needs to exist (a JWT secret) is `generateValue`.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from app.build import scaffold
from app.build.layout import BACKEND, FRONTEND

FRONTEND_ONLY = "frontend"
FULLSTACK = "fullstack"
BACKEND_ONLY = "backend"

#: Vite frameworks build to a folder of static files; Next.js needs a server.
_STATIC = {"react": "dist", "vue": "dist", "svelte": "dist"}
#: What Vercel calls each framework the scaffold detects. Anything else is deployed
#: with no preset, and Vercel's own detection decides.
VERCEL_FRAMEWORK = {"nextjs": "nextjs", "react": "vite", "vue": "vite", "svelte": "vite"}

_API_URL = re.compile(r"(API|BACKEND|SERVER).*URL|URL.*(API|BACKEND|SERVER)")
_CORS = re.compile(r"CORS|ALLOWED_ORIGIN|FRONTEND_URL|CLIENT_URL|ORIGIN")
_GENERATED = re.compile(r"SECRET|TOKEN|SALT|KEY")


def kind(scaffold_info: dict) -> Optional[str]:
    """`frontend`, `fullstack` or `backend` — from what the scaffold found, or None."""
    front = bool(scaffold_info.get("frontend"))
    back = bool(scaffold_info.get("backend"))
    if front and back:
        return FULLSTACK
    if front:
        return FRONTEND_ONLY
    if back:
        return BACKEND_ONLY
    return None


def target(project_kind: Optional[str]) -> Optional[str]:
    """Where "Deploy it" sends this kind: Vercel without GitHub, or GitHub then Render."""
    if project_kind == FRONTEND_ONLY:
        return "vercel"
    if project_kind in (FULLSTACK, BACKEND_ONLY):
        return "render"
    return None


def _side(files: dict[str, str], side: str) -> dict[str, str]:
    prefix = side + "/"
    return {p[len(prefix):]: c for p, c in files.items() if p.startswith(prefix)}


def _env_names(files: dict[str, str]) -> list[str]:
    return scaffold.env_vars(files.items())


def _python_start(backend: dict[str, str], framework: Optional[str]) -> Optional[str]:
    """The scaffold's own run command, made to listen where Render says."""
    entry = scaffold._python_entry(backend, framework)  # noqa: SLF001 - one detector, not two
    if not entry:
        return None
    if entry.startswith("uvicorn "):
        module = entry.split()[1]
        return f"uvicorn {module} --host 0.0.0.0 --port $PORT"
    if entry.startswith("flask "):
        app = entry.split("--app ", 1)[1].split()[0]
        return f"flask --app {app} run --host 0.0.0.0 --port $PORT"
    if "manage.py" in entry:
        return "python manage.py runserver 0.0.0.0:$PORT"
    return None


def _q(value: str) -> str:
    """A YAML scalar that means exactly `value` — a JSON string is valid YAML."""
    return json.dumps(value)


def blueprint(
    files: dict[str, str],
    scaffold_info: dict,
    name: str,
    *,
    saved_env: tuple[str, ...] = (),
    required_env: tuple[str, ...] = (),
) -> Optional[str]:
    """`render.yaml` for a full-stack or backend build; None for a frontend-only one.

    `files` is the assembled tree (`frontend/…`, `backend/…`). `required_env` is the
    charter's database variable names; `saved_env` the ones the user saved values for
    (#54). Only names ever come here — never a value.
    """
    if kind(scaffold_info) not in (FULLSTACK, BACKEND_ONLY):
        return None
    slug = scaffold._slug(name)[:40] or "app"  # noqa: SLF001
    api_name, web_name, db_name = f"{slug}-api", f"{slug}-web", f"{slug}-db"
    front = _side(files, FRONTEND)
    back = _side(files, BACKEND)
    language = scaffold_info.get("backend")
    framework = scaffold_info.get("backend_framework")
    database = scaffold_info.get("database")
    frontend = scaffold_info.get("frontend")
    has_front = bool(frontend) and bool(front)

    # The database: a free Render Postgres only when the build is on plain Postgres
    # and the user hasn't brought their own. Otherwise Render asks for each name.
    contract_names = required_env
    render_postgres = _render_postgres(database, saved_env, required_env)
    back_env = _env_names(back)
    for name_ in (*saved_env, *contract_names):
        if name_ not in back_env:
            back_env.append(name_)
    if render_postgres and "DATABASE_URL" not in back_env:
        back_env.insert(0, "DATABASE_URL")

    migrate = "migrate.py" in back or any(p == "scripts/migrate.cjs" for p in back)
    cors = next((n for n in back_env if _CORS.search(n)), None) if has_front else None
    api_var = next((n for n in _env_names(front) if _API_URL.search(n)), None) if has_front else None

    lines = [
        "# Render Blueprint — written by the platform. It holds variable names only:",
        "# Render asks for every `sync: false` value on its own page, in your account.",
        "services:",
    ]

    # ── the backend ──────────────────────────────────────────────────────────
    lines += [f"  - type: web", f"    name: {_q(api_name)}", "    plan: free", f"    rootDir: {BACKEND}"]
    if language == "python":
        start = _python_start(back, framework) or "python main.py"
        if "migrate.py" in back:
            start = f"python migrate.py && {start}"
        lines += ["    runtime: python", f"    buildCommand: {_q('pip install -r requirements.txt')}"]
    else:
        start = "npm run migrate && npm run start" if migrate else "npm run start"
        lines += ["    runtime: node", f"    buildCommand: {_q('npm install')}"]
    if cors:
        # Render gives the other service's host, not its URL; the scheme goes on here.
        start = f"{cors}=https://$WEB_HOST {start}"
    lines += [f"    startCommand: {_q(start)}", "    autoDeployTrigger: \"off\""]
    env_lines: list[str] = []
    for var in back_env:
        if var == "PORT" or var == cors:
            continue
        if var == "DATABASE_URL" and render_postgres:
            env_lines += [
                f"      - key: {var}",
                "        fromDatabase:",
                f"          name: {_q(db_name)}",
                "          property: connectionString",
            ]
        elif var in saved_env or var in contract_names:
            env_lines += [f"      - key: {var}", "        sync: false"]
        elif _GENERATED.search(var) and not _API_URL.search(var):
            env_lines += [f"      - key: {var}", "        generateValue: true"]
    if cors:
        env_lines += [
            "      - key: WEB_HOST",
            "        fromService:",
            "          type: web",
            f"          name: {_q(web_name)}",
            "          property: host",
        ]
    if env_lines:
        lines += ["    envVars:", *env_lines]

    # ── the frontend ─────────────────────────────────────────────────────────
    if has_front:
        build = "npm install && npm run build"
        if api_var:
            build = f"{api_var}=https://$API_HOST {build}"
        lines += [f"  - type: web", f"    name: {_q(web_name)}", f"    rootDir: {FRONTEND}"]
        if frontend in _STATIC:
            lines += [
                "    runtime: static",
                f"    buildCommand: {_q(build)}",
                f"    staticPublishPath: {_STATIC[frontend]}",
                "    routes:",
                "      - type: rewrite",
                "        source: /*",
                "        destination: /index.html",
            ]
        else:
            # Next.js renders on a server, so it is a Node web service, not static files.
            lines += [
                "    runtime: node",
                "    plan: free",
                f"    buildCommand: {_q(build)}",
                f"    startCommand: {_q('npm run start')}",
            ]
        lines += ["    autoDeployTrigger: \"off\""]
        if api_var:
            lines += [
                "    envVars:",
                "      - key: API_HOST",
                "        fromService:",
                "          type: web",
                f"          name: {_q(api_name)}",
                "          property: host",
            ]

    if render_postgres:
        lines += [
            "databases:",
            f"  - name: {_q(db_name)}",
            "    plan: free",
        ]
    return "\n".join(lines) + "\n"


def _render_postgres(database: Optional[str], saved: tuple[str, ...], required: tuple[str, ...]) -> bool:
    return database == "postgres" and not saved and set(required) <= {"DATABASE_URL"}


def summary(
    files: dict[str, str],
    scaffold_info: dict,
    saved_env: tuple[str, ...] = (),
    required_env: tuple[str, ...] = (),
) -> dict:
    """What the Render step shows before the button: which names Render will ask for,
    and whether a free database is part of it."""
    database = scaffold_info.get("database")
    free = _render_postgres(database, saved_env, required_env)
    asks = [n for n in dict.fromkeys((*saved_env, *required_env)) if not (free and n == "DATABASE_URL")]
    return {
        "asks_for": asks,
        "free_postgres": free,
        "has_frontend": bool(scaffold_info.get("frontend")) and any(p.startswith(FRONTEND + "/") for p in files),
    }
