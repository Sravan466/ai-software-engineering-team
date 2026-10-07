"""Install, build and start what a code phase wrote — for real, in a sandbox (#75).

The compile gate (`check.py`) only parses. This runs the commands the scaffold already
writes into the generated project, so what Vercel or Render would fail on, the crew
fails on first:

  * **Next.js / Vite** — `npm install`, its install scripts with no network, then
    `npm run build` (`next build` / `vite build`);
  * **Node backend** — `npm install`, `npm run build` (every file parses), then the
    entry file started for ten seconds with `PORT` set: a crash is a problem;
  * **Python backend** — `pip install -r requirements.txt`, then the app's entry module
    imported, and `pytest --collect-only` when there are tests.

One interface, `run_build(files, side) -> BuildRun`, whatever runs it: Docker on this
computer, or the `builder` service a hosted (docker-compose) backend talks to. With
neither, the build is `unchecked`, with the reason — and nothing about the Ship gate
changes from the parser-only behaviour.

Failures come back as `check.Problem`s, so the fix loop and its repair prompt take
them unchanged. Builds are capped at `build_run_concurrency` at once, and a project
builds one thing at a time.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional, Protocol

from app.build import buildlog, sandbox
from app.build.check import Problem
from app.build.sandbox import NODE_IMAGE, PYTHON_IMAGE, Limits, Step, StepResult
from app.core.config import settings
from app.core.constants import BuildStatus, TestStatus
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.build import testrun

log = get_logger(__name__)

#: Lines of each step's output kept with the phase, for the page's terminal block.
TAIL_LINES = 40
#: Seconds a Node backend is given to stay up.
BOOT_SECONDS = 10
#: The port a booted backend is told to listen on.
BOOT_PORT = "3999"


# ── what to run ──────────────────────────────────────────────────────────────
@dataclass
class Plan:
    #: nextjs | vite | node | python
    stack: str
    image: str
    steps: list[Step]
    #: The side's manifest, for problems no line of output names a file for.
    manifest: str
    #: A note shown with the result: a step left out, and why.
    note: Optional[str] = None


def _env_example(files: dict[str, str]) -> dict[str, str]:
    """The side's `.env.example`, as the variables a build and a boot are given.

    Placeholders, never a saved value: the sandbox gets exactly what the archive's
    reader would copy into `.env` before running it themselves.
    """
    out: dict[str, str] = {}
    for line in (files.get(".env.example") or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            out[key] = value.strip().strip("'\"")
    return out


def _json(text: Optional[str]) -> dict:
    try:
        data = json.loads(text or "")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


_NPM_INSTALL = Step("install", "npm install", "npm install --ignore-scripts --no-audit --no-fund", network=True)
#: Package install scripts, run where they can't reach anything. A `postinstall` that
#: tries to fetch something — or send something — fails here, and fails the build.
_NPM_SCRIPTS = Step(
    "install",
    "install scripts (offline)",
    "npm rebuild --no-audit --no-fund"
    " && npm run --if-present preinstall && npm run --if-present install"
    " && npm run --if-present postinstall && npm run --if-present prepare",
)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _python_module(files: dict[str, str]) -> Optional[str]:
    """The module whose import starts the app: the one that makes the FastAPI/Flask app."""
    for path in sorted((p for p in files if p.endswith(".py")), key=lambda p: (p.count("/"), p)):
        if "/tests/" in f"/{path}" or path.rsplit("/", 1)[-1].startswith("test_"):
            continue
        if re.search(r"^\w+\s*=\s*(?:FastAPI|Flask)\(", files[path], re.MULTILINE):
            return path[:-3].replace("/", ".")
    for name in ("main.py", "app.py", "app/main.py", "server.py", "src/main.py"):
        if name in files:
            return name[:-3].replace("/", ".")
    return None


def plan_for(files: dict[str, str], side: str) -> tuple[Optional[Plan], Optional[str]]:
    """The steps for one side's files (paths relative to the side). (plan, why none)."""
    env = _env_example(files)
    if "package.json" in files:
        manifest = _json(files["package.json"])
        deps = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}
        scripts = manifest.get("scripts") or {}
        if "next" in deps or "vite" in deps:
            stack = "nextjs" if "next" in deps else "vite"
            if "build" not in scripts:
                return None, "The frontend has no build script, so it was only parsed."
            label = "next build" if stack == "nextjs" else "vite build"
            return Plan(stack, NODE_IMAGE, [_NPM_INSTALL, _NPM_SCRIPTS, Step("build", label, "npm run build", env=env)], "package.json"), None
        if side != "backend":
            return None, "The platform has no build for this frontend yet, so it was only parsed."
        steps = [_NPM_INSTALL, _NPM_SCRIPTS]
        if "build" in scripts:
            steps.append(Step("build", "npm run build", "npm run build", env=env))
        start = str(scripts.get("start") or "")
        entry = start[len("node "):].strip() if start.startswith("node ") else ""
        note = None
        if entry.endswith((".js", ".mjs", ".cjs")) and entry in files:
            steps.append(
                Step(
                    "boot",
                    f"node {entry}",
                    # Up for ten seconds is up: `timeout` ending it is the pass.
                    f"timeout {BOOT_SECONDS} node {_quote(entry)}; code=$?; "
                    f"if [ $code -eq 124 ] || [ $code -eq 143 ]; then echo '[still running after {BOOT_SECONDS} s]'; exit 0; fi; exit $code",
                    timeout=BOOT_SECONDS + 30,
                    env={**env, "PORT": BOOT_PORT, "NODE_ENV": "production"},
                )
            )
        else:
            note = "No JavaScript entry file to start, so the backend was installed and built but not started."
        return Plan("node", NODE_IMAGE, steps, "package.json", note), None
    if "requirements.txt" in files:
        boot_env = {**env, "PYTHONPATH": "/work:/work/.deps", "PORT": BOOT_PORT}
        steps = [
            Step(
                "install",
                "pip install",
                "pip install --no-input --prefer-binary --target /work/.deps -r requirements.txt",
                network=True,
            )
        ]
        note = None
        if "manage.py" in files:
            steps.append(Step("boot", "manage.py check", "python manage.py check", timeout=120, env=boot_env))
        else:
            module = _python_module(files)
            if module:
                code = f"import importlib; importlib.import_module({module!r})"
                steps.append(Step("boot", f"import {module}", f"python -c {_quote(code)}", timeout=120, env=boot_env))
            else:
                note = "No entry module found to import, so the backend was installed but not started."
        tests = [p for p in files if p.endswith(".py") and re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", p)]
        if tests and re.search(r"(?im)^pytest\b", files["requirements.txt"]):
            steps.append(
                Step(
                    "boot",
                    "pytest --collect-only",
                    # 5 is "collected no tests": nothing to run is not a failure here.
                    "python -m pytest --collect-only -q -p no:cacheprovider; code=$?; "
                    "if [ $code -eq 5 ]; then exit 0; fi; exit $code",
                    timeout=120,
                    env=boot_env,
                )
            )
        return Plan("python", PYTHON_IMAGE, steps, "requirements.txt", note), None
    return None, "Nothing here the platform knows how to build yet, so it was only parsed."


# ── what ran it ──────────────────────────────────────────────────────────────
class Engine(Protocol):
    kind: str

    def run(self, image: str, files: dict[str, str], steps: list[Step], limits: Limits,
            on_cancel: Callable[[Callable[[], None]], None],
            on_step: Callable[[Step], None]) -> list[StepResult]: ...


class DockerEngine:
    kind = "docker"

    def run(self, image, files, steps, limits, on_cancel, on_step):
        box = sandbox.Sandbox(image, limits)
        # Before the pull, which can take minutes the first time: Stop pressed during
        # it runs nothing after it.
        on_cancel(box.cancel)
        sandbox.ensure_image(image)
        return box.run(files, steps, on_step=on_step)


class BuilderEngine:
    """The `builder` service: the same sandbox, run where Docker is."""

    kind = "builder"

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def _headers(self) -> dict:
        token = settings.build_runner_token.get_secret_value() if settings.build_runner_token else ""
        return {"Authorization": f"Bearer {token}"} if token else {}

    def run(self, image, files, steps, limits, on_cancel, on_step):
        import httpx

        build_id = uuid.uuid4().hex

        def cancel() -> None:
            try:
                httpx.post(f"{self.url}/cancel", json={"id": build_id}, headers=self._headers(), timeout=10)
            except httpx.HTTPError:
                pass

        on_cancel(cancel)
        on_step(steps[0])
        try:
            r = httpx.post(
                f"{self.url}/run",
                json={"id": build_id, "image": image, "files": files, "steps": [s.as_dict() for s in steps],
                      "limits": limits.as_dict()},
                headers=self._headers(),
                # The budget, plus room for an image pull the builder may need first.
                timeout=limits.seconds + 720,
            )
        except httpx.HTTPError as e:
            raise sandbox.SandboxError(f"The builder service didn't answer ({e.__class__.__name__}).") from e
        if r.status_code != 200:
            detail = ""
            try:
                detail = str((r.json() or {}).get("detail") or "")
            except ValueError:
                detail = r.text[:200]
            raise sandbox.SandboxError(f"The builder service refused the build ({r.status_code}): {detail}"[:300])
        return [StepResult.from_dict(s) for s in (r.json() or {}).get("steps") or []]


#: Tests put a fake engine here; None picks one from the settings and this computer.
engine: Optional[Engine] = None


_health_cache: dict[str, tuple[float, tuple[bool, Optional[str]]]] = {}
#: Seconds a builder service's answer is trusted, as `sandbox.available` trusts Docker's.
_HEALTH_TTL = 30.0


def _builder_health(url: str, refresh: bool = False) -> tuple[bool, Optional[str]]:
    seen = _health_cache.get(url)
    if not refresh and seen is not None and time.monotonic() - seen[0] < _HEALTH_TTL:
        return seen[1]
    answer = _ask_builder(url)
    _health_cache[url] = (time.monotonic(), answer)
    return answer


def _ask_builder(url: str) -> tuple[bool, Optional[str]]:
    import httpx

    try:
        r = httpx.get(f"{url.rstrip('/')}/health", timeout=5)
        data = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return False, f"The builder service at {url} didn't answer."
    if data.get("docker"):
        return True, None
    return False, str(data.get("reason") or "The builder service has no Docker.")


def _engine() -> tuple[Optional[Engine], Optional[str]]:
    if engine is not None:
        return engine, None
    if settings.build_runner_url:
        ok, why = _builder_health(settings.build_runner_url)
        return (BuilderEngine(settings.build_runner_url), None) if ok else (None, why)
    ok, why, _ = sandbox.available()
    return (DockerEngine(), None) if ok else (None, why)


def status() -> dict:
    """What Settings shows: which runner builds, or why none can."""
    if not settings.build_run_enabled:
        return {"kind": "off", "available": False, "reason": "Real builds are switched off (BUILD_RUN_ENABLED=false), so code is only parsed."}
    if engine is not None:
        return {"kind": getattr(engine, "kind", "docker"), "available": True, "reason": None, "detail": None}
    if settings.build_runner_url:
        ok, why = _builder_health(settings.build_runner_url, refresh=True)
        return {"kind": "builder", "available": ok, "reason": why, "detail": settings.build_runner_url,
                "images": list(sandbox.IMAGES)}
    ok, why, version = sandbox.available(refresh=True)
    return {"kind": "docker" if ok else "none", "available": ok, "reason": why,
            "detail": f"Docker {version}" if version else None, "images": list(sandbox.IMAGES)}


# ── the result ───────────────────────────────────────────────────────────────
@dataclass
class BuildRun:
    status: str
    side: str
    stack: Optional[str] = None
    runner: Optional[str] = None
    image: Optional[str] = None
    steps: list[dict] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    reason: Optional[str] = None
    note: Optional[str] = None
    seconds: float = 0.0
    packages: Optional[int] = None
    #: A hash of the side's files as built (#76): a phase kept through a fix round whose
    #: files hash the same is the same build, and isn't built again.
    fingerprint: Optional[str] = None

    @classmethod
    def unchecked(cls, side: str, reason: str, **kw) -> "BuildRun":
        return cls(status=BuildStatus.UNCHECKED.value, side=side, reason=reason, **kw)

    @classmethod
    def from_dict(cls, data: dict) -> "BuildRun":
        return cls(
            status=str(data.get("status") or BuildStatus.UNCHECKED.value),
            side=str(data.get("side") or ""),
            stack=data.get("stack"),
            runner=data.get("runner"),
            image=data.get("image"),
            steps=list(data.get("steps") or []),
            problems=[
                Problem(str(p.get("path") or ""), str(p.get("message") or ""), str(p.get("kind") or "build"),
                        p.get("line"), p.get("step"))
                for p in data.get("problems") or [] if isinstance(p, dict)
            ],
            reason=data.get("reason"),
            note=data.get("note"),
            seconds=float(data.get("seconds") or 0),
            packages=data.get("packages"),
            fingerprint=data.get("fingerprint"),
        )

    def summary(self) -> str:
        """"Installed 212 packages · next build passed in 41 s" — the phase card's line."""
        if self.status == BuildStatus.UNCHECKED.value:
            return self.reason or "Not built."
        ran = [s for s in self.steps if not s.get("skipped")]
        last = next((s for s in reversed(ran) if s["name"] != "install"), ran[-1] if ran else None)
        if self.status == BuildStatus.FAILED.value:
            failed = next((s for s in ran if not s.get("ok") and not s.get("inconclusive")), last)
            n = len(self.problems)
            what = failed["label"] if failed else "The build"
            return f"`{what}` failed · {n} error{'s' if n != 1 else ''}"
        parts = []
        if self.packages:
            parts.append(f"Installed {self.packages} package{'s' if self.packages != 1 else ''}")
        if last is not None and last.get("inconclusive"):
            parts.append(f"`{last['label']}` ran until it reached for its database or the network")
        elif last is not None:
            parts.append(f"`{last['label']}` passed in {round(self.seconds)} s")
        return " · ".join(parts) or f"Built in {round(self.seconds)} s"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "side": self.side,
            "stack": self.stack,
            "runner": self.runner,
            "image": self.image,
            "summary": self.summary(),
            "reason": self.reason,
            "note": self.note,
            "seconds": round(self.seconds, 1),
            "packages": self.packages,
            "steps": self.steps,
            "problems": [p.as_dict() for p in self.problems],
            "fingerprint": self.fingerprint,
            "at": datetime.now(timezone.utc).isoformat(),
        }


def fingerprint(files: dict[str, str]) -> str:
    """One side's files, as a hash: what a build of them depends on."""
    import hashlib

    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode("utf-8") + b"\0" + files[path].encode("utf-8") + b"\0")
    return h.hexdigest()


#: A build worker the memory cap killed: Next prints the signal, V8 its heap.
_OUT_OF_MEMORY = re.compile(r"signal: SIGKILL|JavaScript heap out of memory|Killed\s*$", re.MULTILINE)


def _tail(output: str, lines: int = TAIL_LINES) -> str:
    kept = [l.rstrip() for l in buildlog.clean(output).splitlines()]
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept[-lines:])


def _packages(results: list[StepResult]) -> Optional[int]:
    for r in results:
        if r.name != "install" or not r.output:
            continue
        m = re.search(r"added (\d+) packages?", r.output)
        if m:
            return int(m.group(1))
        m = re.search(r"Successfully installed (.+)", r.output)
        if m:
            return len(m.group(1).split())
    return None


def judge(plan: Plan, results: list[StepResult], side: str, files: dict[str, str]) -> BuildRun:
    """Each step's outcome, read into a status and problems the fix loop can act on."""
    problems: list[Problem] = []
    steps: list[dict] = []
    unchecked: Optional[str] = None
    names = list(files)
    for r in results:
        entry = {
            "name": r.name, "label": r.label, "exit_code": r.exit_code, "seconds": round(r.seconds, 1),
            "ok": r.ok, "timed_out": r.timed_out, "skipped": r.skipped, "tail": _tail(r.output),
        }
        steps.append(entry)
        if r.skipped:
            if r.output and unchecked is None:
                unchecked = r.output
            continue
        if r.timed_out:
            unchecked = unchecked or f"`{r.label}` ran out of time, so the build wasn't finished."
            continue
        if r.ok:
            continue
        if r.exit_code == 125 and re.search(r"^docker: |Error response from daemon", r.output, re.MULTILINE):
            # `docker run` itself failed — a full disk, a daemon error — before the code ran.
            unchecked = unchecked or f"Docker couldn't start `{r.label}`, so the build wasn't finished."
            continue
        if (r.exit_code == 137 or _OUT_OF_MEMORY.search(r.output)) and not (
            buildlog.node_problems(r.output) or buildlog.python_problems(r.output)
        ):
            # Killed — the step, or the build worker under it: the memory cap, not a
            # crash of the code's own.
            unchecked = unchecked or (
                f"`{r.label}` ran out of memory ({settings.build_run_memory_mb} MB), so the build wasn't finished."
            )
            continue
        found: list[Problem] = []
        if r.name == "install":
            if r.label.startswith("install scripts"):
                if buildlog.environmental(r.output):
                    found = [Problem(plan.manifest, "An install script tried to reach the network. Builds have the "
                                                    "network only while packages download; drop the package that needs it.", "package")]
            elif buildlog.environmental(r.output):
                # The download step has the network: failing to reach the registry is
                # the network's fault, whatever pip or npm then says about a package.
                unchecked = unchecked or "The package registry couldn't be reached, so the build wasn't run."
                continue
            elif plan.stack == "python":
                found = buildlog.pip_problems(r.output, plan.manifest)
            else:
                found = buildlog.npm_problems(r.output, plan.manifest)
        elif r.name == "build":
            found = buildlog.js_build_problems(r.output, names) if plan.stack != "python" else buildlog.python_problems(r.output)
            if not found and buildlog.environmental(r.output):
                unchecked = unchecked or f"`{r.label}` needs the network (e.g. Google Fonts), which builds don't have here."
                continue
        else:  # boot
            found = buildlog.python_problems(r.output) if plan.stack == "python" else buildlog.node_problems(r.output)
            # A crash that is itself "can't reach the database" isn't the code's fault in
            # a box with no network; any other crash still is, whatever else it logged.
            found = [p for p in found if not buildlog.environmental(p.message)]
            if not found and buildlog.environmental(r.output):
                # Started as far as its database: a box with no network can't say more.
                entry["inconclusive"] = True
                entry["ok"] = True
                continue
        if not found:
            found = [buildlog.tail_problem(r.output, plan.manifest, f"`{r.label}`")]
        problems += [Problem(p.path, p.message, p.kind, p.line, r.name) for p in found]

    capped = buildlog.capped(problems)
    run = BuildRun(
        status=BuildStatus.FAILED.value if capped else (BuildStatus.UNCHECKED.value if unchecked else BuildStatus.OK.value),
        side=side,
        stack=plan.stack,
        image=plan.image,
        steps=steps,
        problems=buildlog.prefixed(capped, side),
        reason=None if capped else unchecked,
        note=plan.note,
        seconds=sum(r.seconds for r in results),
        packages=_packages(results),
    )
    return run


# ── running one ──────────────────────────────────────────────────────────────
_slots_lock = threading.Lock()
_slots: Optional[threading.BoundedSemaphore] = None
_slots_size = 0
_project_locks: dict[str, threading.Lock] = {}


def _slot() -> threading.BoundedSemaphore:
    global _slots, _slots_size
    with _slots_lock:
        size = max(int(settings.build_run_concurrency), 1)
        if _slots is None or _slots_size != size:
            _slots, _slots_size = threading.BoundedSemaphore(size), size
        return _slots


def _project_lock(project_id: Optional[str]) -> threading.Lock:
    with _slots_lock:
        return _project_locks.setdefault(project_id or "-", threading.Lock())


def _wait(lock, stopped: threading.Event) -> bool:
    """Take `lock`, unless the build is stopped first. True once it's held."""
    while not stopped.is_set():
        if lock.acquire(timeout=0.5):
            return True
    return False


def side_files(files: dict[str, str], side: str) -> dict[str, str]:
    prefix = f"{side}/"
    return {p[len(prefix):]: c for p, c in files.items() if p.startswith(prefix)}


class _Stopped(Exception):
    """The run was stopped while it waited or ran."""


class _CouldntRun(Exception):
    """The runner itself failed — Docker, the builder service — not the code."""


def _execute(image: str, mine: dict[str, str], steps: list[Step], side: str, stage: str,
             chosen: Engine) -> list[StepResult]:
    """Run `steps` over `mine` in the sandbox, queued behind the project's other runs.

    Raises `_Stopped` when Stop was pressed (while queued or running) and `_CouldntRun`
    when the runner failed rather than the code. Shared by the build (#75) and the
    test run (#76), so both queue, stop and cap exactly alike.
    """
    from app.core import identity
    from app.orchestration import activity, claim
    from app.router import inflight

    limits = Limits(
        seconds=float(max(int(settings.build_run_timeout_seconds), 30)),
        memory_mb=max(int(settings.build_run_memory_mb), 256),
        cpus=max(float(settings.build_run_cpus), 0.25),
        cache=identity.current_user_id() or "shared",
    )
    build = inflight.current() or {}
    stopper: list[Callable[[], None]] = []
    call_id = f"build-{uuid.uuid4().hex[:12]}"

    def on_step(step: Step) -> None:
        activity.stage(stage, detail=step.label)

    claim.between_calls()
    activity.stage(stage, detail=steps[0].label)
    results: list[StepResult] = []
    stopped = threading.Event()

    def stop_all() -> None:
        stopped.set()
        for stop in list(stopper):
            stop()

    def on_cancel(stop: Callable[[], None]) -> None:
        # An engine registers its stop as it starts; a Stop that landed a moment
        # before is passed straight on, rather than lost in between.
        stopper.append(stop)
        if stopped.is_set():
            stop()

    # Stop is heard from here on — while the build waits for its turn as well as while
    # it runs. A queued build that was stopped never takes a slot.
    with inflight.track(call_id, stop_all):
        project_lock, slot = _project_lock(build.get("id")), _slot()
        if _wait(project_lock, stopped):
            try:
                if _wait(slot, stopped):
                    try:
                        if not stopped.is_set():
                            results = chosen.run(image, mine, steps, limits, on_cancel, on_step)
                    except Exception as e:  # noqa: BLE001 - the runner must never become the failure
                        if not stopped.is_set():
                            if isinstance(e, sandbox.SandboxError):
                                log.warning("The %s %s couldn't run: %s", side, stage, e)
                                raise _CouldntRun(str(e)) from e
                            log.exception("The %s %s crashed", side, stage)
                            raise _CouldntRun(f"The build runner failed: {e}") from e
                        # Stopped, and the stop is what broke it: settled as a stop below.
                    finally:
                        slot.release()
            finally:
                project_lock.release()
        cancelled = stopped.is_set() or inflight.was_cancelled(call_id)
    if cancelled:
        # Stopped: the run settles it the way it settles a cancelled model call.
        claim.between_calls()
        raise _Stopped()
    return results


def run_build(files: dict[str, str], side: str) -> BuildRun:
    """Install, build and start one side of the tree. Never raises for the build's sake."""
    mine = side_files(files, side)
    plan, why = plan_for(mine, side)
    if plan is None:
        return BuildRun.unchecked(side, why or "Nothing to build.")
    chosen, why = _engine()
    if chosen is None:
        return BuildRun.unchecked(side, why or "No build runner is available.", stack=plan.stack)
    started = time.monotonic()
    try:
        results = _execute(plan.image, mine, plan.steps, side, "building", chosen)
    except _CouldntRun as e:
        return BuildRun.unchecked(side, str(e), stack=plan.stack, runner=chosen.kind)
    except _Stopped:
        return BuildRun.unchecked(side, "The build was stopped.", stack=plan.stack, runner=chosen.kind)
    run = judge(plan, results, side, mine)
    run.runner = chosen.kind
    run.seconds = run.seconds or (time.monotonic() - started)
    run.fingerprint = fingerprint(mine)
    log.info("%s build (%s on %s): %s", side, plan.stack, chosen.kind, run.summary())
    return run


def run_tests(files: dict[str, str], side: str) -> "testrun.TestRun":
    """Install one side and run the tests QA wrote for it (#76). Never raises for the
    suite's sake: what it can't run comes back `not_run`, with the reason."""
    from app.build import testrun

    mine = side_files(files, side)
    tests = [f"{side}/{p}" for p in testrun._test_files(mine)]
    if not tests:
        return testrun.TestRun.not_run(side, "There are no tests to run.")
    # Nothing to run with is "not run", whatever the suite looks like: only a runner
    # that is here can say a suite doesn't run.
    if not settings.build_run_enabled:
        return testrun.TestRun.not_run(side, "Real builds are switched off (BUILD_RUN_ENABLED=false).", files=tests)
    chosen, why = _engine()
    if chosen is None:
        return testrun.TestRun.not_run(side, why or "No test runner is available.", files=tests)
    plan, why, fault = testrun.plan_tests(mine, side)
    if plan is None:
        if fault == testrun.NOTHING:
            return testrun.TestRun.not_run(side, why or "There are no tests to run.", files=tests)
        # A suite that can't run here is never a pass: failed, with the reason. Only
        # one QA can fix (the wrong language) goes back to QA as a problem.
        run = testrun.TestRun(status=TestStatus.FAILED.value, side=side, reason=why, files=tests,
                              runner=chosen.kind)
        if fault == testrun.TESTS_FAULT:
            run.problems = [Problem(tests[0], why or "These tests can't run.", "test", None, "test")]
        return run
    started = time.monotonic()
    try:
        results = _execute(plan.image, mine, plan.steps, side, "testing", chosen)
    except _CouldntRun as e:
        return testrun.TestRun.not_run(side, str(e), framework=plan.framework, runner=chosen.kind, files=tests)
    except _Stopped:
        return testrun.TestRun.not_run(side, "The test run was stopped.", framework=plan.framework,
                                       runner=chosen.kind, files=tests)
    run = testrun.judge(plan, results, side, tests)
    run.runner = chosen.kind
    run.seconds = run.seconds or (time.monotonic() - started)
    log.info("%s tests (%s on %s): %s", side, plan.framework, chosen.kind, run.summary())
    return run


def warm_up() -> None:
    """Pull the images in the background at startup, so no phase waits on a download."""
    if not settings.build_run_enabled or settings.build_runner_url or engine is not None:
        return

    def pull() -> None:
        ok, _, _ = sandbox.available(refresh=True)
        if not ok:
            return
        sandbox.sweep()
        for image in sandbox.IMAGES:
            try:
                sandbox.ensure_image(image)
            except Exception as e:  # noqa: BLE001 - a build will try again and say why
                log.info("Couldn't pull %s ahead of time: %s", image, e)

    threading.Thread(target=pull, name="build-images", daemon=True).start()
