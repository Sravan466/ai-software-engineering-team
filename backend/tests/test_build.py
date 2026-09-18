"""The build: where files go, what the platform writes, and whether it compiles."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.build import layout, toolchain
from app.build.check import check_tree
from app.build.scaffold import build as scaffold_build, initial_migration, platform_owned
from app.orchestration.approval import decide_gate
from app.orchestration.charter import Charter

_parser = toolchain.typescript() is not None and toolchain.node() is not None
needs_parser = pytest.mark.skipif(not _parser, reason="no TypeScript parser available here")


# ── layout ───────────────────────────────────────────────────────────────────
def test_files_are_placed_by_the_side_they_belong_to():
    assert layout.place("backend_engineer", "./main.py") == "backend/main.py"
    assert layout.place("frontend_engineer", "./pages/_app.js") == "frontend/pages/_app.js"
    assert layout.place("frontend_engineer", "frontend/app/page.tsx") == "frontend/app/page.tsx"
    # An agent's own name for its whole side is renamed only when every file uses it...
    # (a README beside the folder does not count against it)...
    placer = layout.Placer("javascript")
    placed = [p for p, *_ in placer.place_all(
        "frontend_engineer",
        [("client/src/App.jsx", ""), ("client/src/main.jsx", ""), ("README.md", "")],
    )]
    assert placed == ["frontend/src/App.jsx", "frontend/src/main.jsx", "frontend/README.md"]
    # ...and QA's tests under the same folder follow the code they test.
    placed = [p for p, *_ in placer.place_all("qa_engineer", [("client/src/App.test.jsx", "")])]
    assert placed == ["frontend/src/App.test.jsx"]
    # `ui/` beside `pages/` is a components folder, and renaming it alone would
    # break `import Button from '../ui/Button'` in the page.
    placed = [p for p, *_ in layout.Placer().place_all(
        "frontend_engineer", [("ui/Button.jsx", ""), ("pages/index.jsx", "")])]
    assert placed == ["frontend/ui/Button.jsx", "frontend/pages/index.jsx"]
    # A side's folder in another case is still that side.
    assert layout.place("frontend_engineer", "Frontend/src/App.jsx") == "frontend/src/App.jsx"
    assert layout.place("qa_engineer", "tests/test_main.py") == "backend/tests/test_main.py"
    assert (
        layout.place("qa_engineer", "tests/form.test.js", "import { render } from '@testing-library/react'")
        == "frontend/tests/form.test.js"
    )
    # Infrastructure stays where DevOps put it, and nothing escapes the archive.
    assert layout.place("devops_engineer", "./docker-compose.yml") == "docker-compose.yml"
    assert layout.place("backend_engineer", "../../etc/passwd") == "backend/etc/passwd"


# ── the compile gate ─────────────────────────────────────────────────────────
def test_python_that_does_not_parse_is_named_with_its_line():
    files = {
        "backend/main.py": "from fastapi import FastAPI\n\ndef broken(:\n    pass\n",
        "backend/ok.py": "import os\nfrom main import app\n",
    }
    result = check_tree(files, list(files))
    assert result.status == "failed"
    [problem] = result.problems
    assert problem.path == "backend/main.py" and problem.kind == "syntax" and problem.line == 3


def test_imports_must_resolve_to_a_file_or_a_package_the_platform_installs():
    files = {
        "backend/app/main.py": "from fastapi import FastAPI\nfrom app.models import User\nfrom .missing import x\nimport frobnicate\n",
        "backend/app/models.py": "class User: ...\n",
        "backend/app/__init__.py": "",
    }
    result = check_tree(files, ["backend/app/main.py"])
    kinds = sorted(p.kind for p in result.problems)
    assert kinds == ["import", "package"]
    assert any("frobnicate" in p.message for p in result.problems)


@needs_parser
def test_a_component_used_without_an_import_does_not_compile():
    files = {
        "frontend/pages/_app.js": (
            "export default function App({ Component, pageProps }) {\n"
            "  return (<div><Header /><Component {...pageProps} /></div>);\n}\n"
        ),
        "frontend/pages/index.jsx": "import Link from 'next/link';\nexport default function Home() { return <Link href='/'>Home</Link>; }\n",
        "frontend/components/Card.jsx": "import { thing } from './nowhere';\nexport default () => <p>{thing}</p>;\n",
    }
    result = check_tree(files, list(files))
    by_path = {p.path: p for p in result.problems}
    assert "Header" in by_path["frontend/pages/_app.js"].message
    assert by_path["frontend/pages/_app.js"].kind == "reference"
    assert by_path["frontend/components/Card.jsx"].kind == "import"
    assert "frontend/pages/index.jsx" not in by_path  # clean code passes


@needs_parser
def test_javascript_that_does_not_parse_is_caught():
    files = {"frontend/app/page.jsx": "export default function Page() { return <main>hi</main>\n"}
    result = check_tree(files, list(files))
    assert result.status == "failed" and result.problems[0].kind == "syntax"


# ── the scaffold ─────────────────────────────────────────────────────────────
def _charter() -> Charter:
    return Charter.from_dict(
        {
            "database": {"token": "postgres", "label": "PostgreSQL", "source": "system_design"},
            "backend_framework": {"token": "fastapi", "label": "FastAPI", "source": "system_design"},
            "language": {"token": "python", "label": "Python", "source": "implied"},
            "frontend_framework": {"token": "nextjs", "label": "Next.js", "source": "system_design"},
        }
    )


def test_the_platform_writes_the_boilerplate_from_what_the_agents_import():
    tree = {
        "backend/main.py": "import os\nfrom fastapi import FastAPI\nimport jwt\napp = FastAPI()\nKEY = os.getenv('JWT_SECRET')\n",
        "frontend/app/page.jsx": (
            "import { useState } from 'react';\nimport axios from 'axios';\nimport Link from 'next/link';\n"
            "export default function Page() { const [n] = useState(0);\n"
            "  return <Link href=\"/x\"><a className=\"btn\">Go {n}</a></Link>; }\n"
        ),
    }
    design = {"data_model": [
        {"entity": "User", "fields": ["id:int", "email:string", "created_at:datetime"]},
        {"entity": "Link", "fields": ["user_id:int", "url:string", "clicks:int"]},
    ]}
    sc = scaffold_build(tree, _charter(), design, "Snip")
    files = {f.path: f.content for f in sc.files}

    root = json.loads(files["package.json"])
    assert root["workspaces"] == ["frontend"] and "build" in root["scripts"]
    frontend = json.loads(files["frontend/package.json"])
    assert {"next", "react", "react-dom", "axios"} <= set(frontend["dependencies"])
    assert "tailwindcss" in frontend["devDependencies"]
    assert "frontend/app/layout.jsx" in files and "frontend/app/globals.css" in files

    requirements = files["backend/requirements.txt"]
    assert "fastapi" in requirements and "uvicorn[standard]" in requirements
    assert "PyJWT" in requirements and "psycopg2-binary" in requirements
    assert "JWT_SECRET=change-me" in files["backend/.env.example"]

    sql = files["backend/migrations/0001_initial.sql"]
    assert sql.index('"users"') < sql.index('"links"')  # referenced table first
    assert 'REFERENCES "users"("id")' in sql
    assert "uvicorn main:app" in " ".join(sc.commands)

    fixed, notes = sc.rewrites["frontend/app/page.jsx"]
    assert fixed.startswith("'use client';")
    assert "<a " not in fixed and 'className="btn"' in fixed
    assert len(notes) == 2


def test_platform_owned_files_are_recognised():
    assert platform_owned("frontend/package.json")
    assert platform_owned("frontend/tailwind.config.ts")
    assert platform_owned("backend/requirements.txt")
    assert not platform_owned("frontend/components/package.jsx")
    assert not platform_owned("backend/app/main.py")


def test_mysql_migrations_use_mysql():
    sql = initial_migration([{"entity": "Order", "fields": ["total:decimal", "paid:bool"]}], "mysql")
    assert "AUTO_INCREMENT" in sql and "DECIMAL(12,2)" in sql and "`orders`" in sql


# ── the gate ─────────────────────────────────────────────────────────────────
def test_a_build_that_does_not_compile_is_never_labelled_finished():
    problems = [{"path": "frontend/pages/_app.js", "line": 8, "kind": "reference", "message": "uses `Header`"}]
    for mode in ("checkpoints", "unattended", "every_phase"):
        project = SimpleNamespace(effective_approval_mode=mode, cost_cap_usd=None)
        gate = decide_gate(project, "cost_estimation", {}, "valid", None, problems)
        assert gate is not None and gate.kind == "build", mode
        assert "frontend/pages/_app.js line 8" in gate.note
    # Mid-run, compile problems do not stop the build — only the finish line does.
    project = SimpleNamespace(effective_approval_mode="unattended", cost_cap_usd=None)
    assert decide_gate(project, "qa_engineer", {}, "valid", None, problems) is None
    # And a build that compiles is an ordinary ship review.
    project = SimpleNamespace(effective_approval_mode="checkpoints", cost_cap_usd=None)
    assert decide_gate(project, "cost_estimation", {}, "valid", None, []).kind == "ship"


def test_a_phase_that_writes_broken_code_is_sent_back_and_recorded(client, monkeypatch):
    """The repair round sees the compile error, and what survives reaches the gate."""
    from app.router.router import router as model_router
    from tests.conftest import _fake_complete

    asked: list[str] = []

    def fake(messages, **kwargs):
        system = messages[0].content
        if system.startswith("You are the Backend Engineer"):
            asked.append(messages[-1].content)
            payload = {
                "framework": "FastAPI",
                "summary": "api",
                "db_models": "none",
                "auth_flow": "none",
                "setup_instructions": ["run it"],
                "files": [{"path": "main.py", "language": "python", "purpose": "app",
                           "code": "from fastapi import FastAPI\napp = FastAPI(\n"}],
            }
            resp = _fake_complete(messages, **kwargs)
            return resp.model_copy(update={"text": json.dumps(payload)})
        return _fake_complete(messages, **kwargs)

    monkeypatch.setattr(model_router, "complete", fake)
    r = client.post("/api/projects", json={"idea": "A broken API", "routing_mode": "local_only",
                                           "approval_mode": "unattended"})
    pid = r.json()["id"]
    client.post(f"/api/projects/{pid}/run")
    project = client.get(f"/api/projects/{pid}").json()

    # Sent back once, with the file and the error named.
    assert len(asked) == 2 and "backend/main.py" in asked[1] and "does not parse" in asked[1]
    backend = next(p for p in project["phases"] if p["phase"] == "backend_engineer")
    assert backend["build_status"] == "failed"
    assert backend["build_note"][0]["path"] == "backend/main.py"
    # Unattended, and still stopped: at the finish, as a build that does not compile.
    assert project["status"] == "awaiting_approval" and project["gate_kind"] == "build"

    art = client.get(f"/api/projects/{pid}/artifacts").json()
    assert art["build"]["status"] == "failed"
    record = next(f for f in art["files"] if f["path"] == "backend/main.py")
    assert record["problems"] and record["phase"] == "backend_engineer"
    assert any(f["phase"] == "platform" and f["path"] == "backend/requirements.txt" for f in art["files"])


def test_files_the_platform_claims_but_does_not_write_are_kept():
    """Only a file the scaffold actually wrote replaces the agent's copy."""
    from datetime import datetime, timezone

    from app.core.artifacts import assemble

    def phase(key, files):
        return SimpleNamespace(
            phase=key, status="approved", created_at=datetime.now(timezone.utc), id=key,
            output={"files": [{"path": p, "code": c} for p, c in files.items()]},
            content_md="", build_status=None, build_note=None,
        )

    project = SimpleNamespace(
        charter=None, name="Vite app", idea="x", phases=[
            phase("backend_engineer", {
                "migrate.py": "print('own runner')\n",
                "alembic.ini": "[alembic]\n",
                "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            }),
            phase("frontend_engineer", {
                "src/main.tsx": "import React from 'react';\n",
                "tsconfig.json": '{"compilerOptions": {"strict": true}}',
                "jest.config.js": "module.exports = {};\n",
                "package-lock.json": "{}",
            }),
        ],
    )
    art = assemble(project)
    by_path = {f["path"]: f for f in art["files"]}
    # Kept: the scaffold writes no Jest config for Vite and no runner for a backend
    # that brings its own migrations.
    assert by_path["frontend/jest.config.js"]["phase"] == "frontend_engineer"
    assert by_path["backend/migrate.py"]["phase"] == "backend_engineer"
    # Replaced: files the scaffold does write, and the lockfile beside its manifest.
    assert by_path["frontend/package.json"]["phase"] == "platform"
    assert by_path["frontend/tsconfig.json"]["phase"] == "platform"
    assert "frontend/package-lock.json" not in by_path
    assert art["scaffold"]["replaced"] == ["frontend/package-lock.json", "frontend/tsconfig.json"]


def test_an_alias_resolves_only_the_way_the_generated_config_says():
    """`@/` means what the scaffold's jsconfig says — not every directory it might."""
    from app.build.check import check_phase

    output = {"files": [
        {"path": "src/app/page.jsx", "code": "import Header from '@/components/Header';\nexport default () => <Header />;\n"},
        {"path": "components/Header.jsx", "code": "export default () => <h1>Hi</h1>;\n"},
    ]}
    result = check_phase({}, "frontend_engineer", output, None)
    # The app lives under src/, so the scaffold maps @/ to ./src/* — and there is no
    # src/components/Header. `next build` would say "Module not found"; so does this.
    assert any(p.kind == "import" and "@/components/Header" in p.message for p in result.problems)


def test_an_agents_other_spelling_of_a_platform_file_is_replaced_too():
    from app.build.scaffold import superseded

    written = {"frontend/jsconfig.json", "frontend/next.config.js", "frontend/package.json"}
    assert superseded("frontend/tsconfig.json", written)  # the other name for the same file
    assert superseded("frontend/next.config.mjs", written)
    assert superseded("frontend/yarn.lock", written)  # pins what the manifest no longer says
    assert not superseded("frontend/jest.config.js", written)  # nothing replaces it
    assert not superseded("backend/tsconfig.json", written)  # another folder, another project


def test_only_the_phases_own_code_decides_its_folder_name():
    placed = [p for p, *_ in layout.Placer().place_all("backend_engineer", [
        ("server/app.py", ""), ("server/routes/users.py", ""),
        ("frontend/index.html", ""), ("docs/api.md", ""),
    ])]
    assert placed == ["backend/app.py", "backend/routes/users.py", "frontend/index.html", "backend/docs/api.md"]


def test_paths_resolve_against_the_final_base_url():
    from app.build.check import aliases_for

    files = {
        "frontend/tsconfig.base.json": '{"compilerOptions": {"paths": {"@/*": ["src/*"]}}}',
        "frontend/tsconfig.json": '{"extends": "./tsconfig.base.json", "compilerOptions": {"baseUrl": "./app"}}',
    }
    assert aliases_for(files, "frontend") == {"@/": ["frontend/app/src"]}


def test_a_top_level_module_keeps_its_imports():
    placed = [p for p, *_ in layout.Placer().place_all("frontend_engineer", [
        ("main.jsx", "import B from './ui/Button'"), ("ui/Button.jsx", ""),
    ])]
    assert placed == ["frontend/main.jsx", "frontend/ui/Button.jsx"]


def test_a_python_backend_is_renamed_file_by_file():
    """Python imports by package: `server/` is the backend root wherever it appears."""
    placer = layout.Placer("python")
    placed = [p for p, *_ in placer.place_all("backend_engineer", [
        ("server/app/main.py", ""), ("tests/test_main.py", "from app.main import app"),
    ])]
    assert placed == ["backend/app/main.py", "backend/tests/test_main.py"]
    placed = [p for p, *_ in placer.place_all("qa_engineer", [("server/tests/test_api.py", "")])]
    assert placed == ["backend/tests/test_api.py"]
