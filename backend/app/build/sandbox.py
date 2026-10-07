"""Running generated code in a throwaway Docker container (#75).

Generated code is untrusted, so it never runs on the API process. Each build gets a
fresh volume holding nothing but its own files — sent in as a tar stream, never a bind
mount of `data/` — and each step runs in its own container on that volume:

  * **install** has the network (to fetch packages) and runs no package scripts: npm
    with `--ignore-scripts`, pip from the platform's own requirements. The scripts run
    in the next container, which has no network — so a `postinstall` that phones home
    fails, and fails the build;
  * **build** and **boot** have no network at all.

Every container is read-only apart from that volume and a small `/tmp`, runs as an
unprivileged user with every capability dropped, under a memory, CPU and process cap,
and is killed when its share of the time budget runs out. The volume is removed after.

Standard library only: this module is also the whole engine of the `builder` service
(`app.build.builder_service`), which runs in its own image beside a hosted backend that
has no Docker of its own.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

#: Images a build may run in. The API names one; the builder service refuses others.
NODE_IMAGE = "node:20-alpine"
PYTHON_IMAGE = "python:3.12-slim"
IMAGES = (NODE_IMAGE, PYTHON_IMAGE)

#: Every container and volume made here carries this label, so leftovers can be found.
LABEL = "aiteam.build=1"
#: The unprivileged user the steps run as (`node` in the Node image).
USER = "1000:1000"
#: Bytes of output kept per step: enough to parse, small enough to keep in memory.
OUTPUT_BYTES = 96_000


class SandboxError(Exception):
    """The sandbox itself failed — Docker, not the generated code."""


@dataclass
class Step:
    #: install | build | boot
    name: str
    #: What a person reads: "npm install", "next build".
    label: str
    #: Run with `sh -c` in /work.
    command: str
    network: bool = False
    #: This step's own cap, inside the build's budget. None: whatever is left.
    timeout: Optional[float] = None
    env: dict = field(default_factory=dict)
    #: Another of `IMAGES` for this step alone — the app preview (#78) installs a Python
    #: backend in the same volume as the Node frontend it serves. None: the sandbox's.
    image: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "command": self.command,
            "network": self.network, "timeout": self.timeout, "env": dict(self.env),
            **({"image": self.image} if self.image else {}),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Step":
        return cls(
            name=str(data.get("name") or "build"),
            label=str(data.get("label") or data.get("name") or "build"),
            command=str(data.get("command") or "true"),
            network=bool(data.get("network")),
            timeout=float(data["timeout"]) if data.get("timeout") else None,
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            image=str(data["image"]) if data.get("image") in IMAGES else None,
        )


@dataclass
class Limits:
    seconds: float = 300.0
    memory_mb: int = 2048
    cpus: float = 2.0
    pids: int = 512
    #: Whose package cache this build reads and fills: per account, so one tenant's
    #: package names are never in another's cache.
    cache: str = "shared"

    def as_dict(self) -> dict:
        return {"seconds": self.seconds, "memory_mb": self.memory_mb, "cpus": self.cpus,
                "pids": self.pids, "cache": self.cache}

    @classmethod
    def from_dict(cls, data: dict) -> "Limits":
        d = cls()
        return cls(
            seconds=float(data.get("seconds") or d.seconds),
            memory_mb=int(data.get("memory_mb") or d.memory_mb),
            cpus=float(data.get("cpus") or d.cpus),
            pids=int(data.get("pids") or d.pids),
            cache=_safe(str(data.get("cache") or d.cache)),
        )


@dataclass
class StepResult:
    name: str
    label: str
    exit_code: Optional[int]
    seconds: float
    output: str = ""
    timed_out: bool = False
    #: Never ran: an earlier step failed, the budget ran out, or the build was stopped.
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return not self.skipped and not self.timed_out and self.exit_code == 0

    def as_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "exit_code": self.exit_code,
            "seconds": round(self.seconds, 1), "output": self.output,
            "timed_out": self.timed_out, "skipped": self.skipped,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StepResult":
        return cls(
            name=str(data.get("name") or ""),
            label=str(data.get("label") or ""),
            exit_code=data.get("exit_code"),
            seconds=float(data.get("seconds") or 0),
            output=str(data.get("output") or "")[-OUTPUT_BYTES:],
            timed_out=bool(data.get("timed_out")),
            skipped=bool(data.get("skipped")),
        )


def _safe(name: str) -> str:
    """A string that can be part of a Docker volume name."""
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in name)[:48].strip("-.")
    return cleaned or "shared"


# ── is Docker here ───────────────────────────────────────────────────────────
_probe_lock = threading.Lock()
_probe: dict = {"at": 0.0, "result": (False, "Docker was not checked yet.", None)}
_PROBE_TTL = 30.0


def docker() -> Optional[str]:
    return shutil.which("docker")


def available(refresh: bool = False) -> tuple[bool, Optional[str], Optional[str]]:
    """(usable, why not, the daemon's version). Asked at most every 30 s."""
    with _probe_lock:
        if not refresh and time.monotonic() - _probe["at"] < _PROBE_TTL and _probe["at"]:
            return _probe["result"]
        cli = docker()
        if cli is None:
            result = (False, "Docker isn't installed on the computer running the backend.", None)
        else:
            try:
                out = subprocess.run(
                    [cli, "version", "--format", "{{.Server.Version}}"],
                    capture_output=True, text=True, timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as e:
                result = (False, f"Docker didn't answer ({e}).", None)
            else:
                if out.returncode == 0 and out.stdout.strip():
                    result = (True, None, out.stdout.strip())
                else:
                    detail = (out.stderr or out.stdout).strip().splitlines()
                    tail = detail[-1][:160] if detail else "no answer"
                    result = (False, f"Docker is installed but not running ({tail}).", None)
        _probe.update(at=time.monotonic(), result=result)
        return result


def ensure_image(image: str, timeout: float = 600.0) -> None:
    """Pull `image` if this daemon doesn't have it. Outside any build's budget."""
    cli = docker()
    if cli is None:
        raise SandboxError("Docker isn't installed.")
    have = subprocess.run([cli, "image", "inspect", image], capture_output=True, timeout=30)
    if have.returncode == 0:
        return
    pulled = subprocess.run([cli, "pull", "--quiet", image], capture_output=True, text=True, timeout=timeout)
    if pulled.returncode != 0:
        raise SandboxError(f"Docker couldn't pull {image}: {(pulled.stderr or '').strip()[-200:]}")


#: A build volume older than this is a crashed run's leftover, never a live build's:
#: every build is killed well inside it (`build_run_timeout_seconds`, capped at 900).
SWEEP_AFTER_SECONDS = 3600


#: What the app preview's sandboxes (#78) carry besides `LABEL`: they outlive a step,
#: so each also names the backend process that runs it — see `sweep_previews`.
PREVIEW_LABEL = "aiteam.preview=1"


def _alive(pid: str) -> bool:
    import os

    try:
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def sweep_previews() -> None:
    """Remove app previews whose backend process is gone.

    A preview lives as long as someone looks at it, held by the backend process that
    started it; when that process dies its containers and volumes are nobody's. Another
    backend running beside this one (a second port, a test run) keeps its own.
    """
    cli = docker()
    if cli is None:
        return
    try:
        for kind, fmt in (("container", '{{.Names}} {{.Label "aiteam.pid"}}'),
                          ("volume", '{{.Name}} {{index .Labels "aiteam.pid"}}')):
            listed = subprocess.run(
                [cli, kind, "ls", *(["-a"] if kind == "container" else []), "--filter", f"label={PREVIEW_LABEL}",
                 "--format", fmt],
                capture_output=True, text=True, timeout=30,
            )
            gone = []
            for line in listed.stdout.splitlines():
                name, _, pid = line.partition(" ")
                if name and not _alive(pid.strip() or "0"):
                    gone.append(name)
            if gone:
                subprocess.run([cli, kind, "rm", "-f", *gone],
                               capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def sweep() -> None:
    """Remove volumes a crashed run left behind — only old ones.

    Between two steps a live build's volume has no container on it, so "not in use"
    doesn't mean "abandoned": another process's build may be mid-way. Each volume
    carries the time it was made, and only ones older than an hour go.
    """
    cli = docker()
    if cli is None:
        return
    try:
        listed = subprocess.run(
            [cli, "volume", "ls", "--filter", f"label={LABEL}",
             "--format", '{{.Name}} {{index .Labels "aiteam.created"}}'],
            capture_output=True, text=True, timeout=30,
        )
        now = time.time()
        old = []
        for line in listed.stdout.splitlines():
            name, _, made = line.partition(" ")
            if name.startswith("aiteam-build-") and made.strip().isdigit() and now - int(made) > SWEEP_AFTER_SECONDS:
                old.append(name)
        if old:
            subprocess.run([cli, "volume", "rm", *old], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


# ── one build ────────────────────────────────────────────────────────────────
def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, content in sorted(files.items()):
            clean = path.strip().lstrip("/")
            if not clean or ".." in clean.split("/"):
                continue
            data = content.encode("utf-8")
            info = tarfile.TarInfo(clean)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Sandbox:
    """One build: a volume, a container per step, and a way to stop it from outside.

    An app preview (#78) is a build that is kept: `run(..., keep=True)` leaves the
    volume in place, `serve` starts a long-lived container on it — with no network
    and no published port, reached only through its stdin and stdout — and `remove`
    takes both away.
    """

    def __init__(self, image: str, limits: Limits, preview: bool = False) -> None:
        if image not in IMAGES:
            raise SandboxError(f"{image} is not an image builds may run in.")
        cli = docker()
        if cli is None:
            raise SandboxError("Docker isn't installed.")
        self.cli = cli
        self.image = image
        self.limits = limits
        self.id = uuid.uuid4().hex[:12]
        self.preview = preview
        self.volume = f"aiteam-{'preview' if preview else 'build'}-{self.id}"
        self.cache = self._cache_for(image)
        self.cancelled = False
        self._current: Optional[str] = None
        self._lock = threading.Lock()
        self._served: list[str] = []

    def _cache_for(self, image: str) -> str:
        kind = "npm" if image == NODE_IMAGE else "pip"
        return f"aiteam-cache-{kind}-{_safe(self.limits.cache)}"

    def _labels(self) -> list[str]:
        import os

        out = ["--label", LABEL]
        if self.preview:
            out += ["--label", PREVIEW_LABEL, "--label", f"aiteam.pid={os.getpid()}"]
        return out

    # ── stopping ─────────────────────────────────────────────────────────────
    def cancel(self) -> None:
        """Stop the build: kill whatever step is running, run nothing after it."""
        with self._lock:
            self.cancelled = True
            name = self._current
        if name:
            self._kill(name)

    def _kill(self, name: str) -> None:
        try:
            subprocess.run([self.cli, "kill", name], capture_output=True, timeout=30)
            subprocess.run([self.cli, "rm", "-f", name], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass

    # ── containers ───────────────────────────────────────────────────────────
    def _args(self, name: str, command: str, *, network: bool, env: dict, root: bool = False,
              image: Optional[str] = None, interactive: bool = False, cache: bool = True) -> list[str]:
        lim = self.limits
        image = image if image in IMAGES else self.image
        args = [
            self.cli, "run", "--rm", *(["-i"] if root or interactive else []), "--name", name, *self._labels(),
            "--network", "bridge" if network else "none",
            "--memory", f"{lim.memory_mb}m", "--memory-swap", f"{lim.memory_mb}m",
            "--cpus", str(lim.cpus), "--pids-limit", str(lim.pids),
            "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=512m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=volume,src={self.volume},dst=/work",
            # A served preview has no use for the package cache, and no business
            # reading the account's other builds' packages.
            *(["--mount", f"type=volume,src={self._cache_for(image)},dst=/cache"] if cache else []),
            "-w", "/work",
            "-e", "HOME=/tmp", "-e", "CI=1",
            "-e", "npm_config_cache=/cache/npm", "-e", "npm_config_update_notifier=false",
            "-e", "PIP_CACHE_DIR=/cache/pip", "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "NEXT_TELEMETRY_DISABLED=1",
        ]
        if root:
            # Only the copy step: it unpacks the files and hands them to the build user.
            args += ["--user", "0:0", "--cap-add", "CHOWN"]
        else:
            args += ["--user", USER]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        return args + [image, "sh", "-c", command]

    def _exec(self, name: str, args: list[str], timeout: float, stdin: Optional[bytes] = None) -> tuple[Optional[int], str, bool]:
        """(exit code, output, timed out)."""
        with self._lock:
            if self.cancelled:
                return None, "", False
            self._current = name
        try:
            proc = subprocess.run(
                args,
                **({"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=max(timeout, 1.0),
            )
        except subprocess.TimeoutExpired as e:
            self._kill(name)
            out = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return None, out[-OUTPUT_BYTES:], True
        finally:
            with self._lock:
                self._current = None
        return proc.returncode, proc.stdout.decode("utf-8", errors="replace")[-OUTPUT_BYTES:], False

    def run(self, files: dict[str, str], steps: list[Step], on_step=None, keep: bool = False) -> list[StepResult]:
        """Put `files` in the volume, then each step in order until one fails.

        `on_step(step)` is told as each step starts, for a progress line. `keep` leaves
        the volume for `serve`; the caller then owns it, and `remove` ends it."""
        if self.cancelled:  # stopped while its image was pulled: nothing runs
            return [StepResult(s.name, s.label, None, 0.0, skipped=True) for s in steps]
        start = time.monotonic()
        deadline = start + self.limits.seconds
        made = subprocess.run(
            [self.cli, "volume", "create", *self._labels(), "--label", f"aiteam.created={int(time.time())}", self.volume],
            capture_output=True, text=True, timeout=60,
        )
        if made.returncode != 0:
            raise SandboxError(f"Docker couldn't make the build's volume: {made.stderr.strip()[-200:]}")
        try:
            code, out, late = self._exec(
                f"{self.volume}-copy",
                self._args(f"{self.volume}-copy",
                           "tar -x -C /work && chown -R 1000:1000 /work && chown 1000:1000 /cache",
                           network=False, env={}, root=True),
                timeout=120,
                stdin=_tar(files),
            )
            if self.cancelled:
                return [StepResult(s.name, s.label, None, 0.0, skipped=True) for s in steps]
            if code != 0:
                raise SandboxError(f"The files couldn't be copied into the sandbox: {out.strip()[-200:]}")
            return self._steps(steps, deadline, on_step)
        finally:
            if not keep:
                self._drop_volume()

    def _steps(self, steps: list[Step], deadline: float, on_step=None, tag: str = "") -> list[StepResult]:
        """Each step in order on the volume, until one fails or the budget runs out."""
        results: list[StepResult] = []
        failed = False
        for i, step in enumerate(steps):
            left = deadline - time.monotonic()
            if failed or self.cancelled or left <= 1:
                results.append(StepResult(step.name, step.label, None, 0.0, skipped=True,
                                          output="" if failed or self.cancelled else "The build ran out of time before this step."))
                failed = True
                continue
            cap = min(left, step.timeout) if step.timeout else left
            name = f"{self.volume}-{tag}{i}"
            if on_step is not None:
                on_step(step)
            began = time.monotonic()
            code, out, late = self._exec(
                name, self._args(name, step.command, network=step.network, env=step.env, image=step.image), cap
            )
            took = time.monotonic() - began
            if self.cancelled and code != 0:
                results.append(StepResult(step.name, step.label, None, took, out, skipped=True))
                failed = True
                continue
            result = StepResult(step.name, step.label, code, took, out, timed_out=late)
            results.append(result)
            failed = not result.ok
        return results

    def more(self, steps: list[Step], seconds: float, on_step=None) -> list[StepResult]:
        """More steps on a kept volume (#78) — the preview's backend, installed once its
        frontend is already being served — with a budget of their own."""
        return self._steps(steps, time.monotonic() + seconds, on_step, tag="more")

    def _drop_volume(self) -> None:
        try:
            subprocess.run([self.cli, "volume", "rm", "-f", self.volume], capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass

    # ── a kept build, served (#78) ───────────────────────────────────────────
    def serve(self, suffix: str, command: str, env: dict, image: Optional[str] = None) -> subprocess.Popen:
        """Start a long-lived container on the kept volume, attached by its stdin and
        stdout. No network, no port: whatever it serves is reached through those pipes
        alone. Its memory and CPU caps are the sandbox's."""
        name = f"{self.volume}-{_safe(suffix)}"
        with self._lock:
            if self.cancelled:
                raise SandboxError("The preview was stopped.")
            self._served.append(name)
        args = self._args(name, command, network=False, env=env, image=image, interactive=True, cache=False)
        try:
            return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            raise SandboxError(f"Docker couldn't start the preview: {e}") from e

    def remove(self) -> None:
        """Stop everything served from this sandbox, then drop its volume."""
        self.cancel()
        with self._lock:
            served = list(self._served)
        for name in served:
            self._kill(name)
        self._drop_volume()
