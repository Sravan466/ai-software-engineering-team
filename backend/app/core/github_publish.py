"""Publish a generated project to a user's own GitHub account.

The OAuth "Connect" flow gives us an access token that belongs to the *user* who
authorized (not the operator). With it we push every generated file in a single
commit via the Git Data API — blobs are created inline by the tree endpoint, so one
tree + one commit + one ref update is all it takes.

The first push creates the repository; every push after that adds a commit to the
same one (the project remembers `github_repo`), built on its current tree, so a
file the user added on GitHub survives. A push that would change nothing adds no
commit.

Tokens are never persisted here — `app.core.deploy_store` holds them, encrypted, and
the route layer hands one in per call. A 401 from GitHub means the user revoked the
app: `GitHubRevoked`, and the route forgets the token.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.core import artifacts, scrub
from app.core.config import settings
from app.db.models import Project

_TIMEOUT = 30.0

#: Tests put an `httpx.MockTransport` here; None is the real network.
transport: Optional[httpx.BaseTransport] = None


class GitHubError(Exception):
    """A user-facing problem talking to GitHub (surfaced as a clean 400)."""


class GitHubRevoked(GitHubError):
    """GitHub no longer accepts the token: the user removed the app's access."""


class GitHubConflict(GitHubError):
    """The repository name is taken on the user's account."""

    def __init__(self, full_name: str, usable: bool) -> None:
        self.full_name = full_name
        #: True when it is an empty repo the user can push to — "Use existing repo".
        self.usable = usable
        super().__init__(
            f"You already have a repository named '{full_name}'. "
            + ("It's empty, so this project can go into it — or pick another name." if usable
               else "Pick another name.")
        )


def _api() -> str:
    return settings.github_api_url.rstrip("/")


def _client(token: Optional[str] = None) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=_api(), headers=headers, timeout=_TIMEOUT, transport=transport)


def _fail(r: httpx.Response, what: str) -> GitHubError:
    if r.status_code == 401:
        return GitHubRevoked("GitHub access was removed — connect GitHub again.")
    try:
        message = r.json().get("message") or ""
    except ValueError:
        message = r.text
    return GitHubError(scrub.scrub(f"GitHub could not {what} ({r.status_code}): {message[:200]}"))


# ── OAuth (with PKCE) ────────────────────────────────────────────────────────
def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) for one OAuth round trip."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(state: str, challenge: str) -> str:
    return f"{settings.github_oauth_url.rstrip('/')}/login/oauth/authorize?" + urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.backend_public_url.rstrip("/") + "/api/github/oauth/callback",
            "scope": settings.github_scope,
            "state": state,
            "allow_signup": "true",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )


def exchange_code(code: str, verifier: str) -> tuple[str, str]:
    """Trade an OAuth `code` (and the PKCE verifier) for (token, granted scopes)."""
    with httpx.Client(timeout=_TIMEOUT, transport=transport) as c:
        r = c.post(
            f"{settings.github_oauth_url.rstrip('/')}/login/oauth/access_token",
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
        )
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise GitHubError(data.get("error_description") or "GitHub did not return an access token.")
    scrub.register(token)
    return token, str(data.get("scope") or "")


def get_user(token: str) -> dict:
    """The authenticated user's public profile (login / name / avatar)."""
    with _client(token) as c:
        r = c.get("/user")
    if r.status_code != 200:
        raise _fail(r, "read your profile")
    return r.json()


# ── Push ─────────────────────────────────────────────────────────────────────
def _empty(c: httpx.Client, full: str) -> bool:
    r = c.get(f"/repos/{full}/commits", params={"per_page": 1})
    return r.status_code == 409 or (r.status_code == 200 and r.json() == [])


def _init_empty(c: httpx.Client, full: str, branch: Optional[str]) -> None:
    """The Git Data API refuses an empty repository; one file through the contents
    API gives it the first commit the tree can be built on."""
    body = {
        "message": "Start the repository",
        "content": base64.b64encode(b"# Generated by the AI Software Engineering Team\n").decode(),
    }
    if branch:
        body["branch"] = branch
    r = c.put(f"/repos/{full}/contents/README.md", json=body)
    if r.status_code not in (200, 201):
        raise _fail(r, "start the empty repository")


def _commit(
    c: httpx.Client, full: str, branch: str, files: dict[str, str], message: str, removed: tuple = ()
) -> tuple[str, bool]:
    """One commit with `files` on top of `branch`. (sha, changed) — no commit when
    the tree would be the same. `removed` are paths to delete in the same commit."""
    ref = c.get(f"/repos/{full}/git/ref/heads/{branch}")
    if ref.status_code in (404, 409) and _empty(c, full):
        _init_empty(c, full, branch)
        ref = c.get(f"/repos/{full}/git/ref/heads/{branch}")
    if ref.status_code != 200:
        raise _fail(ref, f"find the branch '{branch}'")
    head = ref.json()["object"]["sha"]
    cm = c.get(f"/repos/{full}/git/commits/{head}")
    if cm.status_code != 200:
        raise _fail(cm, "read the latest commit")
    base_tree = cm.json()["tree"]["sha"]

    tree = [{"path": p, "mode": "100644", "type": "blob", "content": content} for p, content in files.items()]
    # A null sha with no content deletes the path from the base tree.
    tree += [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in removed if p not in files]
    tr = c.post(f"/repos/{full}/git/trees", json={"base_tree": base_tree, "tree": tree})
    if tr.status_code not in (200, 201):
        raise _fail(tr, "build the file tree")
    tree_sha = tr.json()["sha"]
    if tree_sha == base_tree:
        return head, False

    new = c.post(f"/repos/{full}/git/commits", json={"message": message, "tree": tree_sha, "parents": [head]})
    if new.status_code not in (200, 201):
        raise _fail(new, "make the commit")
    sha = new.json()["sha"]
    up = c.patch(f"/repos/{full}/git/refs/heads/{branch}", json={"sha": sha, "force": False})
    if up.status_code != 200:
        raise _fail(up, f"move '{branch}' to the new commit")
    return sha, True


def push_project(
    token: str,
    project: Project,
    name: Optional[str] = None,
    private: bool = True,
    description: Optional[str] = None,
    use_existing: bool = False,
    assembled: Optional[dict] = None,
    version: Optional[int] = None,
    removed: tuple = (),
) -> dict:
    """Push the whole project to the user's GitHub: into `project.github_repo` when it
    has one, else a new repo — or, with `use_existing`, an empty one of that name.

    `assembled` is the version being pushed (#79), named in the commit as `version`.
    `removed` are files the last push sent that this one doesn't — a version that
    dropped a file, or an older one restored — deleted from a repo this build already
    pushed to. Nothing else in the repo is touched: files a person added stay."""
    if assembled is None:
        assembled = artifacts.assemble(project)
    if not assembled["files"] and not assembled["docs"]:
        raise GitHubError("This project has no generated files to push yet.")
    files = artifacts.ship_files(project, assembled)

    with _client(token) as c:
        repo = None
        if project.github_repo:
            r = c.get(f"/repos/{project.github_repo}")
            if r.status_code == 200:
                repo = r.json()
            elif r.status_code == 401:
                raise _fail(r, "open the repository")
            # 404: deleted or renamed on GitHub since — a fresh one is made below.

        created = False
        if repo is None:
            repo_name = artifacts.slug(name or project.name or project.idea)
            if use_existing:
                login = get_user(token).get("login")
                r = c.get(f"/repos/{login}/{repo_name}")
                if r.status_code != 200:
                    raise _fail(r, f"open '{login}/{repo_name}'")
                repo = r.json()
                if not (repo.get("permissions") or {}).get("push") or not _empty(c, repo["full_name"]):
                    raise GitHubConflict(repo["full_name"], usable=False)
            else:
                desc = (description or project.idea or "")[:300] or "Generated by the AI Software Engineering Team"
                r = c.post(
                    "/user/repos",
                    json={"name": repo_name, "private": private, "description": desc, "auto_init": True},
                )
                if r.status_code == 422:
                    login = get_user(token).get("login")
                    full = f"{login}/{repo_name}"
                    existing = c.get(f"/repos/{full}")
                    usable = (
                        existing.status_code == 200
                        and bool((existing.json().get("permissions") or {}).get("push"))
                        and _empty(c, full)
                    )
                    raise GitHubConflict(full, usable)
                if r.status_code not in (200, 201):
                    raise _fail(r, "create the repository")
                repo = r.json()
                created = True

        full = repo["full_name"]
        branch = repo.get("default_branch") or "main"
        if project.github_repo == full and project.github_branch:
            branch = project.github_branch
        message = (
            "Initial commit — generated by the AI Software Engineering Team"
            if created
            else "Update from AI team build"
        ) + (f" (v{version})" if version else "")
        sha, changed = _commit(c, full, branch, files, message, removed=() if created else tuple(removed))

    return {
        "html_url": repo.get("html_url") or f"https://github.com/{full}",
        "full_name": full,
        "branch": branch,
        "private": bool(repo.get("private")),
        "files": len(files),
        "commit": sha,
        "created": created,
        "changed": changed,
    }
