"""The generated app, running, for the Preview tab (#78).

The Preview tab used to show a second app: a mockup six model passes drew from the
plan, which the person restyled and approved while the code they then downloaded was
something else. This runs the code itself — what `artifacts.assemble` puts in the
archive, built the way the archive builds it — and shows that.

**Where it runs.** In the #75 sandbox, kept rather than thrown away: the frontend is
installed and built (`next build`, `vite build`) in a volume of its own, then served
from a container with no network and no published port. The only way in is the
container's stdin and stdout, which `relay.cjs` turns into requests to the app's own
server inside. Nothing here is reachable except through this backend.

**Where the browser reaches it.** At its own origin, `http://<token>.localhost:8000`,
which this backend answers (`app_proxy`) — never at the API's. Generated code is
untrusted: served from the API's origin, its scripts would run as the signed-in person.
On an origin of its own it has its own storage and cookies, reaches the API as any
other site would (it can't), and is framed only by this app's own pages. The token
is the whole credential and is new every time the app starts.

**How long.** A preview stops after `preview_app_ttl_seconds` without a request, and
at most `preview_app_max_running` run at once (the least recently used stops first).
The project keeps no app — only the code, which is the build's anyway — so opening the
Preview tab again builds it again.

**Where it came from.** The preview build tags every element the frontend renders
with `data-src="frontend/components/Navbar.tsx:12:5"` (`source.py`), so a click on the
preview names a place in the code. The tags are the preview build's alone.

With no Docker, the builder service, or builds switched off, there is no app preview,
and the Preview tab says why and shows the sketch.
"""
from __future__ import annotations

import base64
import itertools
import json
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol
from urllib.parse import urlparse

from app.build import runner as build_runner
from app.build import sandbox
from app.build.sandbox import NODE_IMAGE, PYTHON_IMAGE, Limits, SandboxError, Step
from app.core.config import settings
from app.core.constants import BuildStatus, PhaseStatus
from app.core.logging import get_logger
from app.preview import source

log = get_logger(__name__)

STARTING = "starting"
RUNNING = "running"
FAILED = "failed"
STOPPED = "stopped"

_RELAY = Path(__file__).with_name("relay.cjs")
#: Where the relay lives in the volume, out of the app's own folders.
RELAY_PATH = ".aiteam/relay.cjs"
#: A Python backend's socket, in the volume both containers mount.
API_SOCKET = "/work/.aiteam/api.sock"
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
FRONTEND = "frontend_engineer"


class AppBuildFailed(Exception):
    """The generated app didn't build or start — the code, not the sandbox."""

    def __init__(self, reason: str, problems: Optional[list[dict]] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.problems = problems or []


# ── where it is reached ──────────────────────────────────────────────────────
def preview_domain() -> Optional[str]:
    """The domain previews are served under: `<token>.<domain>`.

    `localhost` when this backend is reached on loopback — every current browser sends
    `*.localhost` there without any DNS — else `PREVIEW_APP_DOMAIN`, a wildcard domain
    that reaches this backend. None when neither is known.
    """
    if settings.preview_app_domain.strip():
        return settings.preview_app_domain.strip().lower().strip(".")
    host = (urlparse(settings.backend_public_url).hostname or "").lower()
    return "localhost" if host in ("localhost", "127.0.0.1", "::1") else None


def origin_for(token: str) -> str:
    parsed = urlparse(settings.backend_public_url)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme or 'http'}://{token}.{preview_domain()}{port}"


def token_from_host(host: Optional[str]) -> Optional[str]:
    """The preview a request's `Host` names, or None when it is this backend's own."""
    domain = preview_domain()
    if not host or not domain:
        return None
    name = host.strip().lower()
    if name.startswith("["):
        return None
    name = name.rsplit(":", 1)[0] if ":" in name else name
    if not name.endswith("." + domain):
        return None
    label = name[: -len(domain) - 1]
    return label if _TOKEN.match(label) else None


# ── can it run here ──────────────────────────────────────────────────────────
def available() -> tuple[bool, Optional[str]]:
    """(whether an app preview can run here, why not)."""
    if not settings.preview_app_enabled:
        return False, "App previews are switched off (PREVIEW_APP_ENABLED=false)."
    if preview_domain() is None:
        return False, (
            "This backend isn't reached on localhost, so set PREVIEW_APP_DOMAIN to a wildcard "
            "domain that reaches it to preview running apps."
        )
    if engine is not None:
        return True, None
    if not settings.build_run_enabled:
        return False, "Real builds are switched off (BUILD_RUN_ENABLED=false), so the app can't run here."
    if settings.build_runner_url:
        return False, "Builds run on the builder service, which doesn't host app previews yet."
    ok, why, _ = sandbox.available()
    return ok, (None if ok else f"App previews need Docker. {why}")


# ── what to run ──────────────────────────────────────────────────────────────
@dataclass
class BackendPlan:
    #: node | python
    kind: str
    steps: list[Step]
    env: dict
    #: node: the entry file; python: `module:app`.
    target: str
    interface: str = "asgi"


@dataclass
class AppPlan:
    #: nextjs | vite
    stack: str
    files: dict[str, str]
    steps: list[Step]
    relay: dict
    routes: list[dict]
    backend: Optional[BackendPlan] = None
    #: Why the backend isn't run, when there is one.
    backend_note: Optional[str] = None
    #: How many elements carry their place in the code; why none do, when none do.
    tagged: int = 0
    tag_note: Optional[str] = None


#: Variables a frontend reads to find its API: pointed at the preview's own `/__api`.
_API_VAR = re.compile(r"(API|BACKEND|SERVER)", re.IGNORECASE)
_API_ROUTE = re.compile(r"^(src/)?(app/api/.+/route|pages/api/.+)\.[cm]?[jt]sx?$")


def _api_var(name: str) -> bool:
    upper = name.upper()
    return bool(_API_VAR.search(upper)) and any(k in upper for k in ("URL", "BASE", "HOST", "ORIGIN", "ENDPOINT"))


def app_routes(front: dict[str, str]) -> list[dict]:
    """The pages a person can open, from the frontend's routing folders — without the
    ones that need a parameter (`/recipes/[id]`), which have no address of their own."""
    found: dict[str, str] = {}
    for path in sorted(front):
        m = re.match(r"^(?:src/)?app/(?:(.*)/)?page\.[jt]sx?$", path)
        if m is not None:
            segs = [s for s in (m.group(1) or "").split("/") if s and not (s.startswith("(") and s.endswith(")"))]
        else:
            m = re.match(r"^(?:src/)?pages/(.+)\.[jt]sx?$", path)
            if m is None:
                continue
            segs = [s for s in m.group(1).split("/") if s]
            if not segs or segs[0] in ("api",) or segs[-1].startswith("_"):
                continue
            if segs[-1] == "index":
                segs = segs[:-1]
        if any("[" in s or s.startswith("@") for s in segs) or (segs and segs[0] == "api"):
            continue
        route = "/" + "/".join(segs)
        title = segs[-1].replace("-", " ").replace("_", " ").title() if segs else "Home"
        found.setdefault(route, title)
    ordered = sorted(found.items(), key=lambda kv: (kv[0] != "/", kv[0].count("/"), kv[0]))
    return [{"path": p, "title": t} for p, t in ordered[:12]]


def _json(text: Optional[str]) -> dict:
    try:
        data = json.loads(text or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


_NEXT_WRAPPER = """// Written by the platform for the app preview only (#78): the build the archive ships,
// without failing on type errors — the real build already judged those.
const base = require('./next.config.aiteam-base.js');
module.exports = Object.assign({}, base, {
  typescript: Object.assign({}, base.typescript, { ignoreBuildErrors: true }),
  eslint: Object.assign({}, base.eslint, { ignoreDuringBuilds: true }),
});
"""


def _python_target(files: dict[str, str]) -> Optional[tuple[str, str]]:
    """(`module:variable`, asgi | wsgi) for the FastAPI or Flask app the backend makes."""
    for path in sorted((p for p in files if p.endswith(".py")), key=lambda p: (p.count("/"), p)):
        if "/tests/" in f"/{path}" or path.rsplit("/", 1)[-1].startswith("test_") or path.startswith(".deps/"):
            continue
        m = re.search(r"^(\w+)\s*=\s*(FastAPI|Flask)\(", files[path], re.MULTILINE)
        if m:
            module = path[:-3].replace("/", ".")
            return f"{module}:{m.group(1)}", ("wsgi" if m.group(2) == "Flask" else "asgi")
    return None


def _backend_plan(back: dict[str, str]) -> tuple[Optional[BackendPlan], Optional[str]]:
    if not back:
        return None, None
    env = build_runner._env_example(back)
    if "package.json" in back:
        scripts = _json(back["package.json"]).get("scripts") or {}
        start = str(scripts.get("start") or "")
        entry = start[len("node "):].strip() if start.startswith("node ") else ""
        if not (entry.endswith((".js", ".mjs", ".cjs")) and entry in back):
            return None, "The backend has no `node <file>` start script, so the preview runs the frontend alone."
        steps = [
            Step("install", "npm install (backend)", "cd backend && npm install --ignore-scripts --no-audit --no-fund",
                 network=True),
            Step("install", "install scripts (backend, offline)",
                 "cd backend && npm rebuild --no-audit --no-fund && npm run --if-present postinstall"),
        ]
        return BackendPlan("node", steps, env, f"/work/backend/{entry}"), None
    if "requirements.txt" in back:
        if "manage.py" in back:
            return None, "Django backends don't run in the preview yet, so it runs the frontend alone."
        target = _python_target(back)
        if target is None:
            return None, "No FastAPI or Flask app was found to start, so the preview runs the frontend alone."
        # The preview has no database server: a throwaway SQLite file stands in. A
        # backend written for one database's own features may not start on it — then
        # the frontend runs alone, and says so.
        if "DATABASE_URL" in env or re.search(r"DATABASE_URL", "\n".join(back.values())):
            env["DATABASE_URL"] = "sqlite:////tmp/preview.db"
        steps = [
            Step("install", "pip install (backend)",
                 "cd backend && pip install --no-input --prefer-binary --target /work/backend/.deps "
                 "-r requirements.txt uvicorn",
                 network=True, image=PYTHON_IMAGE),
        ]
        return BackendPlan("python", steps, env, target[0], target[1]), None
    return None, "The preview doesn't know how to start this backend, so it runs the frontend alone."


def plan_for(files: dict[str, str], origin: str) -> tuple[Optional[AppPlan], Optional[str]]:
    """What to build and serve for one assembled build, or why it can't be."""
    front = build_runner.side_files(files, "frontend")
    if not front:
        return None, "This build has no frontend to run."
    manifest = _json(front.get("package.json"))
    deps = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}
    scripts = manifest.get("scripts") or {}
    if "next" in deps:
        stack = "nextjs"
    elif "vite" in deps:
        stack = "vite"
    else:
        return None, "The preview runs Next.js and Vite frontends, and this one is neither."
    if "build" not in scripts:
        return None, "The frontend has no build script, so it can't be run."

    placed = {f"frontend/{p}": c for p, c in front.items()}
    tagged, count, note = placed, 0, None
    try:
        tagged, count = source.tag(placed)
    except source.Unavailable as e:
        note = f"Elements can't be traced to their code here: {e}"

    env = build_runner._env_example(front)
    for name in list(env):
        if _api_var(name):
            env[name] = f"{origin}/__api"
    env["NEXT_TELEMETRY_DISABLED"] = "1"

    out = {p: c for p, c in tagged.items()}
    if stack == "nextjs" and "frontend/next.config.js" in out:
        out["frontend/next.config.aiteam-base.js"] = out["frontend/next.config.js"]
        out["frontend/next.config.js"] = _NEXT_WRAPPER
    back = build_runner.side_files(files, "backend")
    for path, content in back.items():
        out[f"backend/{path}"] = content
    out[RELAY_PATH] = _RELAY.read_text(encoding="utf-8")

    label = "next build" if stack == "nextjs" else "vite build"
    steps = [
        Step("install", "npm install", "cd frontend && npm install --ignore-scripts --no-audit --no-fund", network=True),
        Step("install", "install scripts (offline)",
             "cd frontend && npm rebuild --no-audit --no-fund && npm run --if-present postinstall"
             " && npm run --if-present prepare"),
        Step("build", label, "cd frontend && npm run build", env=env),
    ]
    relay = {
        "mode": "next" if stack == "nextjs" else "static",
        "root": "/work/frontend",
        "dir": "dist",
        # A frontend with no API routes of its own calls `/api/...` on its backend.
        "apiFallback": not any(_API_ROUTE.match(p) for p in front),
        "env": env,
    }
    backend, backend_note = _backend_plan(back)
    return (
        AppPlan(stack, out, steps, relay, app_routes(front), backend, backend_note, count, note),
        None,
    )


# ── one running app ──────────────────────────────────────────────────────────
class Handle(Protocol):
    def request(self, method: str, path: str, headers: dict, body: bytes, timeout: float = 60.0) -> tuple[int, dict, bytes]: ...
    def alive(self) -> bool: ...
    def stop(self) -> None: ...
    def start_backend(self, plan: BackendPlan, inst: "Instance") -> None: ...


class Engine(Protocol):
    kind: str

    def start(self, inst: "Instance", plan: AppPlan, on_step: Callable[[Step], None]) -> Handle: ...


class Instance:
    """One build of one project's frontend, building or served."""

    def __init__(self, project_id: str, owner_id: Optional[str], built_from: str, built_at: Optional[str]) -> None:
        self.project_id = project_id
        self.owner_id = owner_id
        self.built_from = built_from
        self.built_at = built_at
        self.token = secrets.token_hex(16)
        self.status = STARTING
        self.step = "Queued"
        self.reason: Optional[str] = None
        self.problems: list[dict] = []
        self.stack: Optional[str] = None
        self.routes: list[dict] = []
        self.backend = {"status": "none", "why": ""}
        self.tagged = 0
        self.tag_note: Optional[str] = None
        self.started_at = time.time()
        self.ready_at: Optional[float] = None
        self.last_access = time.monotonic()
        self.handle: Optional[Handle] = None
        self.stopped = False
        self._lock = threading.Lock()
        self._cancel: list[Callable[[], None]] = []

    @property
    def url(self) -> str:
        return origin_for(self.token)

    def touch(self) -> None:
        self.last_access = time.monotonic()

    def alive(self) -> bool:
        return self.status == RUNNING and self.handle is not None and self.handle.alive()

    def on_cancel(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._cancel.append(fn)
            stopped = self.stopped
        if stopped:
            fn()

    def set_backend(self, status: str, why: str = "") -> None:
        self.backend = {"status": status, "why": why or ""}

    def request(self, method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
        self.touch()
        if self.handle is None or not self.handle.alive():
            raise SandboxError("The preview isn't running.")
        return self.handle.request(method, path, headers, body)

    def stop(self) -> None:
        with self._lock:
            if self.stopped:
                return
            self.stopped = True
            cancels = list(self._cancel)
        for fn in cancels:
            try:
                fn()
            except Exception:  # noqa: BLE001 - stopping must finish
                log.exception("Stopping a preview step failed")
        if self.handle is not None:
            try:
                self.handle.stop()
            except Exception:  # noqa: BLE001
                log.exception("Stopping a preview failed")
        if self.status in (STARTING, RUNNING):
            self.status = STOPPED


# ── Docker ───────────────────────────────────────────────────────────────────
class _Gone(Exception):
    pass


class RelayHandle:
    """The served container, spoken to over its stdin and stdout (`relay.cjs`)."""

    def __init__(self, proc, box: sandbox.Sandbox, inst: Instance) -> None:
        self.proc = proc
        self.box = box
        self.inst = inst
        self.ready = threading.Event()
        self.failed: Optional[str] = None
        self.log: deque[str] = deque(maxlen=120)
        self._pending: dict[int, list] = {}
        self._lock = threading.Lock()
        self._write = threading.Lock()
        self._ids = itertools.count(1)
        self._backend_proc = None
        threading.Thread(target=self._read, name=f"preview-out-{inst.project_id[:8]}", daemon=True).start()
        threading.Thread(target=self._read_err, name=f"preview-err-{inst.project_id[:8]}", daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind is None and msg.get("id") is not None:
                with self._lock:
                    slot = self._pending.pop(int(msg["id"]), None)
                if slot is not None:
                    slot[1] = msg
                    slot[0].set()
            elif kind == "ready":
                self.ready.set()
            elif kind == "failed":
                self.failed = str(msg.get("why") or "The app stopped.")
                self.ready.set()
            elif kind == "backend":
                self.inst.set_backend(str(msg.get("status") or "none"), str(msg.get("why") or ""))
        self.failed = self.failed or "The preview stopped."
        self.ready.set()
        with self._lock:
            pending, self._pending = self._pending, {}
        for slot in pending.values():
            slot[0].set()

    def _read_err(self) -> None:
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self.log.append(line[:400])

    def tail(self) -> str:
        lines = [l for l in self.log if l.strip()]
        return lines[-1][:240] if lines else ""

    def send(self, msg: dict) -> None:
        data = (json.dumps(msg) + "\n").encode("utf-8")
        with self._write:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise _Gone(str(e)) from e

    def request(self, method, path, headers, body, timeout=60.0):
        n = next(self._ids)
        slot: list = [threading.Event(), None]
        with self._lock:
            self._pending[n] = slot
        try:
            self.send({
                "id": n, "method": method, "path": path, "headers": headers,
                "body": base64.b64encode(body).decode("ascii") if body else "",
            })
        except _Gone:
            with self._lock:
                self._pending.pop(n, None)
            raise SandboxError("The preview isn't running.")
        if not slot[0].wait(timeout):
            with self._lock:
                self._pending.pop(n, None)
            return 504, {"content-type": "text/plain; charset=utf-8"}, b"The app took too long to answer."
        msg = slot[1]
        if msg is None:
            raise SandboxError("The preview stopped.")
        if msg.get("error") and not msg.get("body"):
            return int(msg.get("status") or 502), {"content-type": "text/plain; charset=utf-8"}, (
                f"The app's server didn't answer: {msg['error']}".encode("utf-8")
            )
        body_out = base64.b64decode(msg.get("body") or "")
        return int(msg.get("status") or 502), dict(msg.get("headers") or {}), body_out

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        self.box.remove()
        for proc in (self.proc, self._backend_proc):
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass

    # ── the backend, once the frontend is up ─────────────────────────────────
    def start_backend(self, plan: BackendPlan, inst: Instance) -> None:
        inst.set_backend("installing")
        results = self.box.more(plan.steps, seconds=float(settings.preview_app_build_seconds))
        if inst.stopped:
            return
        bad = next((r for r in results if not r.ok), None)
        if bad is not None:
            inst.set_backend("down", f"`{bad.label}` failed: {_last_line(bad.output) or 'see the build log'}")
            self._tell_backend("down", inst.backend["why"])
            return
        if plan.kind == "node":
            self.send({"type": "start-backend", "entry": plan.target, "cwd": "/work/backend", "env": plan.env})
            return
        command = (
            f"cd /work/backend && exec python -m uvicorn {plan.target} --uds {API_SOCKET}"
            + (" --interface wsgi" if plan.interface == "wsgi" else "")
        )
        env = {**plan.env, "PYTHONPATH": "/work/backend:/work/backend/.deps"}
        try:
            self._backend_proc = self.box.serve("api", command, env, image=PYTHON_IMAGE)
        except SandboxError as e:
            inst.set_backend("down", str(e))
            self._tell_backend("down", str(e))
            return
        self.send({"type": "backend-socket", "socket": API_SOCKET, "seconds": 45})
        threading.Thread(target=self._watch_backend, args=(inst,), name="preview-api", daemon=True).start()

    def _watch_backend(self, inst: Instance) -> None:
        proc = self._backend_proc
        tail: deque[str] = deque(maxlen=40)
        for raw in proc.stderr:
            tail.append(raw.decode("utf-8", errors="replace").rstrip())
        proc.wait()
        if inst.stopped:
            return
        why = _last_line("\n".join(tail)) or f"It exited with code {proc.returncode}."
        inst.set_backend("down", why)
        self._tell_backend("down", why)

    def _tell_backend(self, status: str, why: str) -> None:
        try:
            self.send({"type": "backend", "status": status, "why": why})
        except _Gone:
            pass


def _last_line(text: str) -> str:
    from app.build import buildlog

    lines = [l.strip() for l in buildlog.clean(text or "").splitlines() if l.strip()]
    return lines[-1][:240] if lines else ""


class DockerEngine:
    kind = "docker"

    def start(self, inst: Instance, plan: AppPlan, on_step) -> Handle:
        limits = Limits(
            seconds=float(max(int(settings.preview_app_build_seconds), 60)),
            memory_mb=max(int(settings.preview_app_memory_mb), 512),
            cpus=max(float(settings.build_run_cpus), 0.25),
            cache=inst.owner_id or "shared",
        )
        box = sandbox.Sandbox(NODE_IMAGE, limits, preview=True)
        inst.on_cancel(box.remove)
        sandbox.ensure_image(NODE_IMAGE)
        if plan.backend is not None and plan.backend.kind == "python":
            sandbox.ensure_image(PYTHON_IMAGE)
        try:
            results = box.run(plan.files, plan.steps, on_step=on_step, keep=True)
        except BaseException:
            box.remove()
            raise
        if inst.stopped:
            box.remove()
            raise SandboxError("The preview was stopped.")
        if not all(r.ok for r in results):
            box.remove()
            build_plan = build_runner.Plan(plan.stack, NODE_IMAGE, plan.steps, "frontend/package.json")
            run = build_runner.judge(build_plan, results, "frontend", build_runner.side_files(plan.files, "frontend"))
            if run.status == BuildStatus.UNCHECKED.value:
                raise SandboxError(run.reason or "The preview build didn't finish.")
            raise AppBuildFailed(f"The app didn't build: {run.summary()}", [p.as_dict() for p in run.problems][:8])
        on_step(Step("serve", "starting the server", "true"))
        proc = box.serve("app", f"node /work/{RELAY_PATH}", {"AITEAM_PREVIEW": json.dumps(plan.relay)})
        handle = RelayHandle(proc, box, inst)
        if not handle.ready.wait(120) or handle.failed:
            why = handle.failed or "The app's server didn't start in time."
            tail = handle.tail()
            handle.stop()
            raise AppBuildFailed(f"{why}{f' {tail}' if tail and tail not in why else ''}".strip())
        return handle


#: Tests put a fake engine here; None runs the app in Docker.
engine: Optional[Engine] = None


def _engine() -> Engine:
    return engine if engine is not None else DockerEngine()


# ── which apps are running ───────────────────────────────────────────────────
_lock = threading.RLock()
#: project -> the app its Preview tab shows.
_serving: dict[str, Instance] = {}
#: project -> a newer build of it, until it is ready to take over.
_building: dict[str, Instance] = {}
#: token -> served app.
_tokens: dict[str, Instance] = {}
_build_slots = threading.BoundedSemaphore(2)
_reaper: Optional[threading.Thread] = None


def lookup(token: Optional[str]) -> Optional[Instance]:
    if not token:
        return None
    with _lock:
        inst = _tokens.get(token)
    if inst is None or inst.stopped:
        return None
    return inst


def serving(project_id: str) -> Optional[Instance]:
    with _lock:
        return _serving.get(project_id)


def building(project_id: str) -> Optional[Instance]:
    with _lock:
        return _building.get(project_id)


def _retire(inst: Instance) -> None:
    with _lock:
        if _serving.get(inst.project_id) is inst:
            _serving.pop(inst.project_id, None)
        if _building.get(inst.project_id) is inst:
            _building.pop(inst.project_id, None)
        _tokens.pop(inst.token, None)
    inst.stop()


def stop(project_id: str) -> None:
    with _lock:
        found = [i for i in (_serving.get(project_id), _building.get(project_id)) if i is not None]
    for inst in found:
        _retire(inst)


def stop_all() -> None:
    with _lock:
        everything = list(_serving.values()) + list(_building.values())
    for inst in everything:
        _retire(inst)


def _ensure_reaper() -> None:
    global _reaper
    with _lock:
        if _reaper is not None and _reaper.is_alive():
            return
        _reaper = threading.Thread(target=_reap_forever, name="preview-reaper", daemon=True)
        _reaper.start()


def reap() -> list[Instance]:
    """Stop every app nobody has looked at for the TTL, and every one that died."""
    ttl = max(int(settings.preview_app_ttl_seconds), 30)
    now = time.monotonic()
    with _lock:
        idle = [i for i in _serving.values() if now - i.last_access > ttl or not i.alive()]
    for inst in idle:
        crashed = inst.alive() is False and not inst.stopped and now - inst.last_access <= ttl
        log.info("Stopping the app preview of %s (%s).", inst.project_id, "it stopped" if crashed else "idle")
        _retire(inst)
        if crashed and inst.ready_at and time.time() - inst.ready_at < 60:
            # Died as soon as it started: the code, not a timeout. Don't build it again
            # by itself; say why.
            tail = inst.handle.tail() if isinstance(inst.handle, RelayHandle) else ""
            _remember_failure(inst, f"The app stopped right after it started. {tail}".strip(), [])
    return idle


def _reap_forever() -> None:
    while True:
        time.sleep(15)
        try:
            reap()
        except Exception:  # noqa: BLE001 - the reaper must keep running
            log.exception("The preview reaper failed")


def _cap() -> None:
    """At most `preview_app_max_running` apps: the least recently looked at stop first."""
    limit = max(int(settings.preview_app_max_running), 1)
    with _lock:
        running = sorted(_serving.values(), key=lambda i: i.last_access)
    for inst in running[: max(len(running) - limit, 0)]:
        log.info("Stopping the app preview of %s to make room.", inst.project_id)
        _retire(inst)


# ── the project's side ───────────────────────────────────────────────────────
def current_frontend(project) -> tuple[Optional[object], bool]:
    """(the Frontend attempt that counts, whether a newer one is being written now)."""
    rows = [r for r in project.phases if r.phase == FRONTEND]
    busy = any(r.status == PhaseStatus.RUNNING.value for r in rows)
    done = [
        r for r in rows
        if r.status not in (PhaseStatus.REJECTED.value, PhaseStatus.FAILED.value, PhaseStatus.RUNNING.value)
        and isinstance(r.output, dict) and r.output.get("files")
    ]
    done.sort(key=lambda r: (r.created_at, r.id))
    return (done[-1] if done else None), busy


def _built_at(row) -> Optional[str]:
    at = getattr(row, "completed_at", None) or getattr(row, "created_at", None)
    if at is None:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.isoformat()


def _remember_failure(inst: Instance, reason: str, problems: list[dict]) -> None:
    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.preview import app_state

    db = SessionLocal()
    try:
        project = db.get(Project, inst.project_id)
        if project is not None:
            app_state.record_failure(project, inst.built_from, reason, problems)
            db.commit()
    except Exception:  # noqa: BLE001 - only a note for the page
        log.exception("Couldn't record a failed app preview for %s", inst.project_id)
    finally:
        db.close()


def _forget_failure(project_id: str) -> None:
    from app.db.base import SessionLocal
    from app.db.models import Project
    from app.preview import app_state

    db = SessionLocal()
    try:
        project = db.get(Project, project_id)
        if project is not None and app_state.failed(project) is not None:
            app_state.clear_failure(project)
            db.commit()
    finally:
        db.close()


def ensure(project, *, retry: bool = False) -> Optional[Instance]:
    """Start the app for the project's current frontend, unless it runs or is starting.

    Returns what is starting or served for that attempt, or None when nothing can be.
    `retry` builds again an attempt whose last preview build failed.
    """
    from app.preview import app_state

    ok, _why = available()
    if not ok:
        return None
    row, busy = current_frontend(project)
    if row is None or busy:
        return None
    run = row.build_run if isinstance(row.build_run, dict) else None
    if run is not None and run.get("status") == BuildStatus.FAILED.value:
        return None  # the real build failed: the code doesn't build, here or anywhere
    known = app_state.failed(project)
    if known and known.get("row") == row.id and not retry:
        return None
    with _lock:
        current = _serving.get(project.id)
        if current is not None and current.built_from == row.id and current.alive():
            current.touch()
            return current
        pending = _building.get(project.id)
        if pending is not None and pending.built_from == row.id and not pending.stopped:
            return pending
        inst = Instance(project.id, project.owner_id, row.id, _built_at(row))
        _building[project.id] = inst
    if pending is not None:
        _retire(pending)
    _ensure_reaper()
    threading.Thread(target=_start, args=(inst,), name=f"preview-{project.id[:8]}", daemon=True).start()
    return inst


def _start(inst: Instance) -> None:
    from app.core import artifacts, identity
    from app.db.base import SessionLocal
    from app.db.models import Project

    db = SessionLocal()
    try:
        project = db.get(Project, inst.project_id)
        if project is None:
            _retire(inst)
            return
        inst.step = "Assembling the code"
        with identity.acting_as(project.owner_id):
            assembled = artifacts.assemble(project)
    except Exception as e:  # noqa: BLE001 - the page says why
        log.exception("Couldn't assemble %s for its app preview", inst.project_id)
        _fail(inst, f"The build couldn't be assembled: {e}", persist=False)
        return
    finally:
        db.close()

    files = {f["path"]: f["content"] for f in assembled.get("files") or [] if isinstance(f.get("content"), str)}
    plan, why = plan_for(files, inst.url)
    if plan is None:
        _fail(inst, why or "This frontend can't be run.", persist=True)
        return
    inst.stack, inst.routes, inst.tagged, inst.tag_note = plan.stack, plan.routes, plan.tagged, plan.tag_note
    if plan.backend is None and plan.backend_note:
        inst.set_backend("none", plan.backend_note)

    inst.step = "Waiting for a free build slot"
    while not _build_slots.acquire(timeout=0.5):
        if inst.stopped:
            return
    try:
        if inst.stopped:
            return
        handle = _engine().start(inst, plan, on_step=lambda step: setattr(inst, "step", step.label))
    except AppBuildFailed as e:
        _fail(inst, e.reason, e.problems, persist=True)
        return
    except SandboxError as e:
        _fail(inst, str(e), persist=False)
        return
    except Exception as e:  # noqa: BLE001 - never strand the page on "starting"
        log.exception("The app preview of %s crashed", inst.project_id)
        _fail(inst, f"The preview couldn't start: {e}", persist=False)
        return
    finally:
        _build_slots.release()

    if inst.stopped:
        handle.stop()
        return
    inst.handle = handle
    inst.status = RUNNING
    inst.step = ""
    inst.ready_at = time.time()
    inst.touch()
    with _lock:
        old = _serving.get(inst.project_id)
        _serving[inst.project_id] = inst
        if _building.get(inst.project_id) is inst:
            _building.pop(inst.project_id, None)
        _tokens[inst.token] = inst
    if old is not None and old is not inst:
        _retire(old)
    log.info("App preview of %s is up (%s, %d elements traced to code).", inst.project_id, plan.stack, plan.tagged)
    _forget_failure(inst.project_id)
    _cap()
    if plan.backend is not None:
        threading.Thread(
            target=_start_backend, args=(inst, plan.backend), name=f"preview-api-{inst.project_id[:8]}", daemon=True
        ).start()


def _start_backend(inst: Instance, plan: BackendPlan) -> None:
    try:
        inst.handle.start_backend(plan, inst)
    except Exception as e:  # noqa: BLE001 - the frontend still runs
        log.warning("The backend of %s's preview didn't start: %s", inst.project_id, e)
        inst.set_backend("down", f"It couldn't be started: {e}")


def _fail(inst: Instance, reason: str, problems: Optional[list[dict]] = None, *, persist: bool) -> None:
    inst.status = FAILED
    inst.reason = reason
    inst.problems = problems or []
    inst.step = ""
    log.info("App preview of %s didn't start: %s", inst.project_id, reason)
    with _lock:
        if _building.get(inst.project_id) is inst:
            _building.pop(inst.project_id, None)
    if persist:
        _remember_failure(inst, reason, inst.problems)
    else:
        # Not the code's fault: kept only in memory, so the page can say why and a
        # second look tries again.
        _transient[inst.project_id] = (inst.built_from, reason, time.monotonic())


#: project -> (attempt, why, when) for a start that failed for the sandbox's sake.
_transient: dict[str, tuple[str, str, float]] = {}


# ── what the page is told ────────────────────────────────────────────────────
def state(project, *, touch: bool = False, current: Optional[tuple] = None) -> dict:
    """The app preview's state for `PreviewOut.app`. No side effects but `touch`.
    `current`: `current_frontend(project)`, when the caller has it."""
    from app.preview import app_state

    row, busy = current or current_frontend(project)
    ok, why = available()
    serving_now = serving(project.id)
    building_now = building(project.id)
    if touch and serving_now is not None:
        serving_now.touch()
    alive = serving_now is not None and serving_now.alive()
    out: dict = {
        "status": "none",
        "url": serving_now.url if alive else None,
        "built_from": serving_now.built_from if alive else None,
        "built_at": serving_now.built_at if alive else None,
        "current_from": row.id if row is not None else None,
        "stale": bool(alive and row is not None and serving_now.built_from != row.id),
        "step": None,
        "reason": None,
        "problems": [],
        "stack": (serving_now.stack if alive else None),
        "routes": serving_now.routes if alive else [],
        "backend": dict(serving_now.backend) if alive else {"status": "none", "why": ""},
        "traced": (serving_now.tagged if alive else 0),
        "trace_note": serving_now.tag_note if alive else None,
        "ttl_seconds": int(settings.preview_app_ttl_seconds),
    }
    if row is None:
        out["status"] = "none"
        out["reason"] = (
            "The crew is writing the frontend now." if busy else "The crew hasn't written the frontend yet."
        )
        return out
    if not ok:
        out["status"] = "unavailable"
        out["reason"] = why
        return out
    run = row.build_run if isinstance(row.build_run, dict) else None
    known = app_state.failed(project)
    transient = _transient.get(project.id)
    if building_now is not None and not building_now.stopped and building_now.status == STARTING:
        out["status"] = STARTING
        out["step"] = building_now.step
    elif run is not None and run.get("status") == BuildStatus.FAILED.value:
        out["status"] = FAILED
        out["reason"] = f"The app didn't build: {run.get('summary') or 'see the Build tab'}"
        out["problems"] = [p for p in run.get("problems") or [] if isinstance(p, dict)][:8]
    elif known and known.get("row") == row.id:
        out["status"] = FAILED
        out["reason"] = known.get("reason")
        out["problems"] = list(known.get("problems") or [])
    elif alive and serving_now.built_from == row.id:
        out["status"] = RUNNING
    elif transient and transient[0] == row.id and time.monotonic() - transient[2] < 60:
        out["status"] = FAILED
        out["reason"] = transient[1]
    elif busy:
        out["status"] = "waiting"
        out["reason"] = "The crew is changing the frontend; the app starts again once it's done."
    else:
        out["status"] = "idle"
    return out
