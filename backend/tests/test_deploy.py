"""#55: "Deploy it" (Vercel for a frontend, GitHub → Render for anything with a backend)
and "Connect to GitHub" — against stand-ins for GitHub and Vercel, never the network."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.api.routes import github as github_routes
from app.build import blueprint, dbconnect
from app.core import deploy_store, github_publish, project_secrets, vercel
from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import PhaseResult, Project
from tests.conftest import TEST_USER_ID

GOOD_VERCEL = "vercel_tok_GOOD_0123456789abcd"
GH_TOKEN = "gho_SENTINELghTOKEN0123456789"
DB_PASSWORD = "Sentinel-Pw-7c1d9e"


# ── builds ───────────────────────────────────────────────────────────────────
_REACT = {
    "files": [
        {"path": "src/main.jsx", "code": "import React from 'react';\nimport App from './App';\n"},
        {"path": "src/App.jsx", "code": "export default function App(){ const u = import.meta.env.VITE_SUPABASE_URL; const a = import.meta.env.VITE_API_URL; return <p>{u}{a}</p>; }\n"},
    ]
}
_FASTAPI = {
    "files": [
        {"path": "main.py", "code": "import os\nfrom fastapi import FastAPI\napp = FastAPI()\nSECRET = os.getenv('JWT_SECRET')\nORIGINS = os.getenv('CORS_ORIGINS')\nDB = os.getenv('DATABASE_URL')\n"},
    ]
}
_NEXT = {
    "files": [
        {"path": "app/page.jsx", "code": "import Link from 'next/link';\nexport default function Page(){ return <Link href='/'>hi</Link>; }\n"},
    ]
}
_EXPRESS = {
    "files": [
        {"path": "server.js", "code": "const express = require('express');\nconst app = express();\napp.listen(process.env.PORT);\n"},
    ]
}


def _charter(front: str, back: str | None, database: str | None, provider: str | None = None) -> dict:
    out: dict = {"frontend_framework": {"token": front, "label": front, "source": "design"}}
    if back:
        out["backend_framework"] = {"token": back, "label": back, "source": "design"}
    if database:
        out["database"] = {"token": database, "label": database, "source": "design"}
        if provider:
            out["database_provider"] = provider
            out["env"] = list(dbconnect.env_names(database, provider))
    return out


def _project(front: dict | None, back: dict | None, charter: dict | None = None, status: str = "completed") -> str:
    with SessionLocal() as db:
        p = Project(idea="A shared grocery list", name="Grocery Pal", owner_id=TEST_USER_ID, status=status, charter=charter)
        db.add(p)
        db.flush()
        for phase, out in (("backend_engineer", back), ("frontend_engineer", front)):
            if out is not None:
                db.add(PhaseResult(project_id=p.id, phase=phase, agent=phase, status="approved", output=out))
        db.commit()
        return p.id


def _row(pid: str) -> Project:
    with SessionLocal() as db:
        p = db.get(Project, pid)
        db.expunge(p)
        return p


# ── stand-ins ────────────────────────────────────────────────────────────────
class FakeGitHub:
    def __init__(self) -> None:
        self.valid = {GH_TOKEN}
        self.repos: dict[str, dict] = {}
        self.trees: dict[str, dict[str, str]] = {"t0": {}}
        self.commits: dict[str, dict] = {}
        self.bodies: list[str] = []
        self.token_requests: list[dict] = []

    def _tree_sha(self, files: dict[str, str]) -> str:
        sha = hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()
        self.trees[sha] = dict(files)
        return sha

    def _commit(self, tree: str, parents: list[str]) -> str:
        sha = hashlib.sha1(f"{tree}{parents}{len(self.commits)}".encode()).hexdigest()
        self.commits[sha] = {"tree": tree, "parents": parents}
        return sha

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode() if request.content else ""
        self.bodies.append(body)
        path = request.url.path
        if path == "/login/oauth/access_token":
            self.token_requests.append(parse_qs(body))
            return httpx.Response(200, json={"access_token": GH_TOKEN, "scope": "repo"})
        auth = request.headers.get("authorization", "")
        if auth.removeprefix("Bearer ") not in self.valid:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if path == "/user":
            return httpx.Response(200, json={"login": "ada", "name": "Ada", "avatar_url": "https://x/a.png"})
        if path == "/user/repos" and request.method == "POST":
            data = json.loads(body)
            full = f"ada/{data['name']}"
            if full in self.repos:
                return httpx.Response(422, json={"message": "name already exists on this account"})
            head = self._commit(self._tree_sha({"README.md": "starter"}), [])
            self.repos[full] = {"private": data["private"], "head": head}
            return httpx.Response(201, json=self._repo(full))
        parts = path.split("/")
        if parts[1] != "repos":
            return httpx.Response(404, json={})
        full = f"{parts[2]}/{parts[3]}"
        repo = self.repos.get(full)
        if repo is None:
            return httpx.Response(404, json={"message": "Not Found"})
        rest = "/".join(parts[4:])
        if rest == "":
            return httpx.Response(200, json=self._repo(full))
        if rest == "commits":
            return httpx.Response(409 if repo["head"] is None else 200, json=[] if repo["head"] is None else [{}])
        if rest == "contents/README.md" and request.method == "PUT":
            repo["head"] = self._commit(self._tree_sha({"README.md": "start"}), [])
            return httpx.Response(201, json={})
        if rest.startswith("git/ref/heads/"):
            if repo["head"] is None:
                return httpx.Response(409, json={"message": "Git Repository is empty."})
            return httpx.Response(200, json={"object": {"sha": repo["head"]}})
        if rest.startswith("git/commits/") and request.method == "GET":
            return httpx.Response(200, json={"tree": {"sha": self.commits[parts[-1]]["tree"]}})
        if rest == "git/trees":
            data = json.loads(body)
            files = dict(self.trees[data["base_tree"]])
            for e in data["tree"]:
                if "content" in e:
                    files[e["path"]] = e["content"]
                elif e.get("sha", "") is None:
                    files.pop(e["path"], None)  # a null sha deletes the path
            return httpx.Response(201, json={"sha": self._tree_sha(files)})
        if rest == "git/commits":
            data = json.loads(body)
            return httpx.Response(201, json={"sha": self._commit(data["tree"], data["parents"])})
        if rest.startswith("git/refs/heads/"):
            repo["head"] = json.loads(body)["sha"]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    def _repo(self, full: str) -> dict:
        return {
            "full_name": full,
            "html_url": f"https://github.com/{full}",
            "default_branch": "main",
            "private": self.repos[full]["private"],
            "permissions": {"push": True},
        }

    def files(self, full: str) -> dict[str, str]:
        return self.trees[self.commits[self.repos[full]["head"]]["tree"]]

    def commit_count(self, full: str) -> int:
        n, sha = 0, self.repos[full]["head"]
        while sha:
            n += 1
            parents = self.commits[sha]["parents"]
            sha = parents[0] if parents else None
        return n


class FakeVercel:
    def __init__(self) -> None:
        self.stored: set[str] = set()
        self.uploads: list[bytes] = []
        self.env: list[dict] = []
        self.deployments: list[dict] = []
        self.state = "BUILDING"
        self.bodies: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {GOOD_VERCEL}":
            return httpx.Response(403, json={"error": {"code": "forbidden", "message": "Not authorized"}})
        path = request.url.path
        if path == "/v2/files":
            self.uploads.append(request.content)
            self.stored.add(request.headers["x-vercel-digest"])
            return httpx.Response(200, json={})
        body = request.content.decode() if request.content else ""
        self.bodies.append(body)
        if path == "/v2/user":
            return httpx.Response(200, json={"user": {"username": "ada-v"}})
        if path == "/v11/projects":
            return httpx.Response(200, json={})
        if path.startswith("/v10/projects/"):
            self.env.extend(json.loads(body))
            return httpx.Response(201, json={})
        if path == "/v13/deployments":
            data = json.loads(body)
            missing = [f["sha"] for f in data["files"] if f["sha"] not in self.stored]
            if missing:
                return httpx.Response(400, json={"error": {"code": "missing_files", "missing": missing}})
            self.deployments.append(data)
            return httpx.Response(200, json={"id": "dpl_1", "url": f"{data['name']}-abc123.vercel.app", "readyState": "QUEUED"})
        if path == "/v13/deployments/dpl_1":
            return httpx.Response(
                200,
                json={"id": "dpl_1", "readyState": self.state, "url": "grocery-pal-abc123.vercel.app",
                      "alias": ["grocery-pal.vercel.app"], "errorMessage": "Command \"npm run build\" exited with 1"},
            )
        if path == "/v3/deployments/dpl_1/events":
            return httpx.Response(200, json=[{"type": "stdout", "text": "> vite build"}, {"type": "stderr", "text": f"Error: token {GOOD_VERCEL} leaked"}])
        return httpx.Response(404, json={})


@pytest.fixture
def fakes(monkeypatch):
    gh, vc = FakeGitHub(), FakeVercel()
    monkeypatch.setattr(github_publish, "transport", httpx.MockTransport(gh))
    monkeypatch.setattr(vercel, "transport", httpx.MockTransport(vc))
    monkeypatch.setattr(settings, "github_client_id", "cid")
    monkeypatch.setattr(settings, "github_client_secret", "csecret")
    store = deploy_store.for_user(TEST_USER_ID)
    for kind in (deploy_store.GITHUB, deploy_store.VERCEL):
        store.remove(kind)
    github_routes._revoked.clear()
    yield gh, vc
    for kind in (deploy_store.GITHUB, deploy_store.VERCEL):
        store.remove(kind)


def _connect_github():
    deploy_store.for_user(TEST_USER_ID).save(deploy_store.GITHUB, GH_TOKEN, login="ada")


def _connect_vercel(client):
    r = client.put("/api/deploy/vercel/token", json={"token": GOOD_VERCEL}, headers={"host": "localhost"})
    assert r.status_code == 200 and r.json()["applied"], r.text


# ── what kind of build it is ─────────────────────────────────────────────────
def test_the_kind_comes_from_what_the_scaffold_found(client):
    front = _project(_REACT, None, _charter("react", None, None))
    full = _project(_REACT, _FASTAPI, _charter("react", "fastapi", "postgres", "generic"))
    back = _project(None, _FASTAPI, _charter("react", "fastapi", None))
    kinds = {pid: client.get(f"/api/projects/{pid}/ship").json() for pid in (front, full, back)}
    assert (kinds[front]["kind"], kinds[front]["target"]) == ("frontend", "vercel")
    assert (kinds[full]["kind"], kinds[full]["target"]) == ("fullstack", "render")
    assert (kinds[back]["kind"], kinds[back]["target"]) == ("backend", "render")


def test_a_react_fastapi_blueprint_has_a_static_site_an_api_and_a_free_database(client):
    pid = _project(_REACT, _FASTAPI, _charter("react", "fastapi", "postgres", "generic"))
    from app.core import artifacts

    files = artifacts.ship_files(_row_with_phases(pid), artifacts.assemble(_row_with_phases(pid)))
    render = files["render.yaml"]
    assert "runtime: static" in render and "staticPublishPath: dist" in render
    assert "uvicorn main:app --host 0.0.0.0 --port $PORT" in render
    assert "fromDatabase" in render and "databases:" in render and "plan: free" in render
    assert "key: JWT_SECRET\n        generateValue: true" in render
    assert "export CORS_ORIGINS=https://$WEB_HOST && python migrate.py && uvicorn main:app" in render
    assert "export VITE_API_URL=https://$API_HOST && npm install && npm run build" in render
    assert ".env\"" not in json.dumps(sorted(files)) and "backend/.env" not in files


def test_a_next_express_blueprint_runs_next_on_a_node_server():
    info = {"frontend": "nextjs", "backend": "javascript", "backend_framework": "express", "database": None}
    render = blueprint.blueprint(
        {"frontend/app/page.jsx": "x", "backend/server.js": "x", "backend/package.json": "{}"}, info, "Shop"
    )
    assert render.count("runtime: node") == 2 and "npm run start" in render and "databases:" not in render


def test_a_frontend_only_build_gets_no_blueprint():
    assert blueprint.blueprint({"frontend/src/App.jsx": "x"}, {"frontend": "react"}, "x") is None


def test_saved_database_values_are_asked_for_by_render_never_written(client, fakes):
    pid = _project(_REACT, _FASTAPI, _charter("react", "fastapi", "mongodb", "atlas"))
    contract = dbconnect.contract_for("mongodb", "atlas")
    uri = f"mongodb+srv://ada:{DB_PASSWORD}@cluster0.abcde.mongodb.net/app"
    project_secrets.save(TEST_USER_ID, pid, contract, {"MONGODB_URI": uri}, dbconnect.CheckResult(dbconnect.UNCHECKED, "saved"))
    ship = client.get(f"/api/projects/{pid}/ship").json()
    assert ship["render"]["asks_for"] == ["MONGODB_URI"] and ship["render"]["free_postgres"] is False
    gh, _ = fakes
    _connect_github()
    r = client.post(f"/api/projects/{pid}/deploy", json={"name": "grocery-pal"})
    assert r.status_code == 200, r.text
    pushed = gh.files("ada/grocery-pal")
    assert "key: MONGODB_URI\n        sync: false" in pushed["render.yaml"]
    everything = "\n".join(gh.bodies) + r.text + client.get(f"/api/projects/{pid}/ship").text
    assert DB_PASSWORD not in everything and GH_TOKEN not in r.text


# ── Vercel: the token ────────────────────────────────────────────────────────
def test_a_rejected_vercel_token_is_never_saved_and_never_replaces_a_good_one(client, fakes):
    r = client.put("/api/deploy/vercel/token", json={"token": "vercel_tok_BAD_000000000000"}, headers={"host": "localhost"})
    assert r.status_code == 200 and r.json()["applied"] is False and r.json()["reason"] == "rejected"
    assert not deploy_store.for_user(TEST_USER_ID).public()["vercel"]["connected"]

    _connect_vercel(client)
    client.put("/api/deploy/vercel/token", json={"token": "vercel_tok_BAD_000000000000"}, headers={"host": "localhost"})
    public = client.get("/api/deploy/connections").json()["vercel"]
    assert public == {**public, "connected": True, "username": "ada-v", "hint": "…abcd"}
    raw = deploy_store.for_user(TEST_USER_ID).path.read_text()
    assert GOOD_VERCEL not in raw and "enc:v1:" in raw
    assert oct(deploy_store.for_user(TEST_USER_ID).path.stat().st_mode & 0o777) == "0o600"
    assert GOOD_VERCEL not in client.get("/api/deploy/connections").text


def test_a_vercel_token_is_only_saved_from_this_backends_own_address(client, fakes):
    r = client.put("/api/deploy/vercel/token", json={"token": GOOD_VERCEL}, headers={"host": "evil.example"})
    assert r.status_code == 403


# ── Vercel: deploying ────────────────────────────────────────────────────────
def test_a_frontend_deploys_to_vercel_and_redeploys_to_the_same_url(client, fakes):
    _, vc = fakes
    pid = _project(_REACT, None, _charter("react", None, None))
    r = client.post(f"/api/projects/{pid}/deploy", json={})
    assert r.status_code == 409 and r.json()["needs"] == "vercel"

    _connect_vercel(client)
    r = client.post(f"/api/projects/{pid}/deploy", json={})
    assert r.status_code == 200, r.text
    first_uploads = len(vc.uploads)
    assert first_uploads > 0 and vc.deployments[0]["name"] == "grocery-pal"
    assert vc.deployments[0]["projectSettings"]["framework"] == "vite"
    assert all(not f["file"].startswith("frontend/") for f in vc.deployments[0]["files"])
    assert client.get(f"/api/projects/{pid}/deploy").json()["status"] == "building"

    vc.state = "READY"
    done = client.get(f"/api/projects/{pid}/deploy").json()
    assert done["status"] == "ready" and done["url"] == "https://grocery-pal.vercel.app"
    assert _row(pid).deploy_url == "https://grocery-pal.vercel.app"

    r = client.post(f"/api/projects/{pid}/deploy", json={})
    assert r.status_code == 200
    assert len(vc.uploads) == first_uploads, "a redeploy uploaded files Vercel already had"
    assert vc.deployments[1]["name"] == vc.deployments[0]["name"]


def test_a_failed_vercel_build_shows_the_scrubbed_log(client, fakes):
    _, vc = fakes
    pid = _project(_REACT, None, _charter("react", None, None))
    _connect_vercel(client)
    client.post(f"/api/projects/{pid}/deploy", json={})
    vc.state = "ERROR"
    r = client.get(f"/api/projects/{pid}/deploy")
    body = r.json()
    assert body["status"] == "error" and "Build failed" in body["error"]
    assert "> vite build" in body["log"] and GOOD_VERCEL not in r.text
    # #75: the whole log is kept, scrubbed, and stays on screen on the next poll too.
    kept = _row(pid).deploy_log
    assert kept and "> vite build" in kept and all(GOOD_VERCEL not in line for line in kept)
    again = client.get(f"/api/projects/{pid}/deploy").json()
    assert again["log"] == body["log"]
    # This build was never run by the pipeline here — there is no run to rewind — so
    # the log is shown and nothing is sent back. `test_build_runner.py` covers the
    # build that is.
    assert again["fix"] is None and _row(pid).status == "completed"


def test_only_public_database_values_reach_vercel(client, fakes):
    _, vc = fakes
    pid = _project(_REACT, None, _charter("react", None, "postgres", "supabase"))
    contract = dbconnect.contract_for("postgres", "supabase")
    project_secrets.save(
        TEST_USER_ID,
        pid,
        contract,
        {
            "SUPABASE_URL": "https://abcdefghijklmnop.supabase.co",
            "SUPABASE_ANON_KEY": "sb_publishable_abc123456789",
            "DATABASE_URL": f"postgresql://postgres.ref:{DB_PASSWORD}@aws-0.pooler.supabase.com:5432/postgres",
        },
        dbconnect.CheckResult(dbconnect.UNCHECKED, "saved"),
    )
    _connect_vercel(client)
    assert client.post(f"/api/projects/{pid}/deploy", json={}).status_code == 200
    assert {e["key"]: e["value"] for e in vc.env} == {"VITE_SUPABASE_URL": "https://abcdefghijklmnop.supabase.co"}
    sent = "\n".join(vc.bodies) + "\n".join(u.decode() for u in vc.uploads)
    assert DB_PASSWORD not in sent


def test_deploy_guards(client, fakes, monkeypatch):
    running = _project(_REACT, None, _charter("react", None, None), status="running")
    assert client.post(f"/api/projects/{running}/deploy", json={}).status_code == 409

    _connect_vercel(client)
    pid = _project(_REACT, None, _charter("react", None, None))
    with SessionLocal() as db:
        db.get(Project, pid).deploy_status = "building"
        db.commit()
    r = client.post(f"/api/projects/{pid}/deploy", json={})
    assert r.status_code == 409 and "already running" in r.json()["detail"]

    monkeypatch.setattr(settings, "deploys_per_hour", 1)
    other = _project(_REACT, None, _charter("react", None, None))
    assert client.post(f"/api/projects/{other}/deploy", json={}).status_code == 200
    again = _project(_REACT, None, _charter("react", None, None))
    assert client.post(f"/api/projects/{again}/deploy", json={}).status_code == 429


def test_an_interrupted_upload_is_reported_not_left_running(client, fakes):
    pid = _project(_REACT, None, _charter("react", None, None))
    with SessionLocal() as db:
        db.get(Project, pid).deploy_status = "uploading"
        db.commit()
    body = client.get(f"/api/projects/{pid}/deploy").json()
    assert body["status"] == "error" and "interrupted" in body["error"]


# ── GitHub ───────────────────────────────────────────────────────────────────
def test_connecting_github_uses_pkce_and_survives_a_restart(client, fakes):
    gh, _ = fakes
    r = client.get("/api/github/oauth/start", params={"return_to": "http://localhost:3000/projects/x"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0]
    state = q["state"][0]
    r = client.get("/api/github/oauth/callback", params={"code": "c1", "state": state}, follow_redirects=False)
    assert "github=connected" in r.headers["location"]
    verifier = gh.token_requests[0]["code_verifier"][0]
    import base64

    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert expected == q["code_challenge"][0]

    # A "restart": nothing in memory, only what is on disk.
    github_routes._pending.clear()
    status = client.get("/api/github/status").json()
    assert status["connected"] and status["login"] == "ada"
    raw = deploy_store.for_user(TEST_USER_ID).path.read_text()
    assert GH_TOKEN not in raw and GH_TOKEN not in json.dumps(status)


def test_a_callback_for_another_state_is_refused(client, fakes):
    r = client.get("/api/github/oauth/callback", params={"code": "c1", "state": "forged"}, follow_redirects=False)
    assert "github=error" in r.headers["location"]
    assert not client.get("/api/github/status").json()["connected"]


def test_a_second_push_adds_a_commit_to_the_same_repo(client, fakes):
    gh, _ = fakes
    _connect_github()
    pid = _project(_REACT, None, _charter("react", None, None))
    r = client.post(f"/api/github/push/{pid}", json={"name": "grocery-pal"})
    assert r.status_code == 200 and r.json()["created"]
    assert gh.repos["ada/grocery-pal"]["private"] is True
    assert _row(pid).github_repo == "ada/grocery-pal"
    assert gh.commit_count("ada/grocery-pal") == 2

    with SessionLocal() as db:
        ph = db.query(PhaseResult).filter_by(project_id=pid).first()
        ph.output = {"files": [*_REACT["files"], {"path": "src/extra.js", "code": "export const x = 1;\n"}]}
        db.commit()
    r = client.post(f"/api/github/push/{pid}", json={"name": "ignored-name"})
    assert r.status_code == 200 and not r.json()["created"] and r.json()["changed"]
    assert r.json()["full_name"] == "ada/grocery-pal" and gh.commit_count("ada/grocery-pal") == 3
    assert "frontend/src/extra.js" in gh.files("ada/grocery-pal")

    r = client.post(f"/api/github/push/{pid}", json={})
    assert r.json()["changed"] is False and gh.commit_count("ada/grocery-pal") == 3


def test_a_taken_name_offers_the_repo_only_when_it_is_empty(client, fakes):
    gh, _ = fakes
    _connect_github()
    gh.repos["ada/grocery-pal"] = {"private": True, "head": None}
    pid = _project(_REACT, None, _charter("react", None, None))
    r = client.post(f"/api/github/push/{pid}", json={"name": "grocery-pal"})
    assert r.status_code == 409 and r.json()["conflict"] == {"full_name": "ada/grocery-pal", "usable": True}
    r = client.post(f"/api/github/push/{pid}", json={"name": "grocery-pal", "use_existing": True})
    assert r.status_code == 200, r.text
    assert "frontend/src/App.jsx" in gh.files("ada/grocery-pal")

    other = _project(_REACT, None, _charter("react", None, None))
    r = client.post(f"/api/github/push/{other}", json={"name": "grocery-pal"})
    assert r.status_code == 409 and r.json()["conflict"]["usable"] is False


def test_a_revoked_github_token_turns_into_reconnect(client, fakes):
    gh, _ = fakes
    _connect_github()
    gh.valid.clear()
    pid = _project(_REACT, None, _charter("react", None, None))
    r = client.post(f"/api/github/push/{pid}", json={"name": "grocery-pal"})
    assert r.status_code == 409 and "connect GitHub again" in r.json()["detail"]
    status = client.get("/api/github/status").json()
    assert status["connected"] is False and status["reason"] == "revoked"


def test_full_stack_deploy_asks_for_github_then_hands_off_to_render(client, fakes):
    gh, _ = fakes
    pid = _project(_REACT, _FASTAPI, _charter("react", "fastapi", "postgres", "generic"))
    r = client.post(f"/api/projects/{pid}/deploy", json={})
    assert r.status_code == 409 and r.json()["needs"] == "github"
    _connect_github()
    r = client.post(f"/api/projects/{pid}/deploy", json={})
    assert r.status_code == 409 and r.json()["needs"] == "push"
    r = client.post(f"/api/projects/{pid}/deploy", json={"name": "grocery-pal", "private": True})
    assert r.status_code == 200, r.text
    assert r.json()["handoff_url"] == "https://render.com/deploy?repo=https://github.com/ada/grocery-pal"
    assert "render.yaml" in gh.files("ada/grocery-pal")
    assert _row(pid).deploy_status == "handed_off"

    r = client.put(f"/api/projects/{pid}/deploy/url", json={"url": "grocery-pal-api.onrender.com/"})
    assert r.json()["url"] == "https://grocery-pal-api.onrender.com"
    assert client.put(f"/api/projects/{pid}/deploy/url", json={"url": "javascript:alert(1)"}).status_code == 422


def test_the_new_columns_are_added_to_an_existing_database(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from app.db.migrations import run_migrations

    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id VARCHAR(32) PRIMARY KEY, idea TEXT)"))
        conn.execute(text("INSERT INTO projects (id, idea) VALUES ('a', 'old build')"))
    applied = run_migrations(engine)
    for name in ("github_repo", "deploy_url", "deploy_status", "deployed_at", "deploy_error"):
        assert f"projects.{name}" in applied
    cols = {c["name"] for c in inspect(engine).get_columns("projects")}
    assert {"github_branch", "github_pushed_at", "deploy_target", "deploy_id"} <= cols
    assert run_migrations(engine) == [], "a second run must change nothing"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT idea, github_repo FROM projects")).fetchall() == [("old build", None)]


def _row_with_phases(pid: str) -> Project:
    db = SessionLocal()
    return db.get(Project, pid)


def test_times_reach_the_page_as_utc_with_their_offset(client, fakes):
    _connect_github()
    pid = _project(_REACT, None, _charter("react", None, None))
    client.post(f"/api/github/push/{pid}", json={"name": "grocery-pal"})
    pushed = client.get(f"/api/projects/{pid}/ship").json()["github_pushed_at"]
    assert pushed.endswith("+00:00"), "a naive time reads as local time in the browser"


def test_a_build_from_before_the_database_question_still_has_render_ask_for_its_connection():
    info = {"frontend": None, "backend": "python", "backend_framework": None, "database": "mongodb"}
    render = blueprint.blueprint(
        {"backend/app.py": "import os\nURI = os.getenv('MONGODB_URI')\nKEY = os.getenv('API_SECRET')\n"}, info, "Links"
    )
    assert "key: MONGODB_URI\n        sync: false" in render
    assert "key: API_SECRET\n        generateValue: true" in render
    assert 'startCommand: "python app.py"' in render
