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

    def as_dict(self) -> dict:
        return {
            "name": self.name, "label": self.label, "command": self.command,
            "network": self.network, "timeout": self.timeout, "env": dict(self.env),
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


def sweep() -> None:
    """Remove volumes a crashed run left behind. In-use volumes are refused by Docker."""
    cli = docker()
    if cli is None:
        return
    try:
        listed = subprocess.run(
            [cli, "volume", "ls", "-q", "--filter", f"label={LABEL}"], capture_output=True, text=True, timeout=30
        )
        names = [n for n in listed.stdout.split() if n.startswith("aiteam-build-")]
        if names:
            subprocess.run([cli, "volume", "rm", *names], capture_output=True, timeout=60)
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
    """One build: a volume, a container per step, and a way to stop it from outside."""

    def __init__(self, image: str, limits: Limits) -> None:
        if image not in IMAGES:
            raise SandboxError(f"{image} is not an image builds may run in.")
        cli = docker()
        if cli is None:
            raise SandboxError("Docker isn't installed.")
        self.cli = cli
        self.image = image
        self.limits = limits
        self.id = uuid.uuid4().hex[:12]
        self.volume = f"aiteam-build-{self.id}"
        kind = "npm" if image == NODE_IMAGE else "pip"
        self.cache = f"aiteam-cache-{kind}-{_safe(limits.cache)}"
        self.cancelled = False
        self._current: Optional[str] = None
        self._lock = threading.Lock()

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
    def _args(self, name: str, command: str, *, network: bool, env: dict, root: bool = False) -> list[str]:
        lim = self.limits
        args = [
            self.cli, "run", "--rm", *(["-i"] if root else []), "--name", name, "--label", LABEL,
            "--network", "bridge" if network else "none",
            "--memory", f"{lim.memory_mb}m", "--memory-swap", f"{lim.memory_mb}m",
            "--cpus", str(lim.cpus), "--pids-limit", str(lim.pids),
            "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=512m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--mount", f"type=volume,src={self.volume},dst=/work",
            "--mount", f"type=volume,src={self.cache},dst=/cache",
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
        return args + [self.image, "sh", "-c", command]

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

    def run(self, files: dict[str, str], steps: list[Step], on_step=None) -> list[StepResult]:
        """Put `files` in the volume, then each step in order until one fails.

        `on_step(step)` is told as each step starts, for a progress line."""
        if self.cancelled:  # stopped while its image was pulled: nothing runs
            return [StepResult(s.name, s.label, None, 0.0, skipped=True) for s in steps]
        start = time.monotonic()
        deadline = start + self.limits.seconds
        made = subprocess.run(
            [self.cli, "volume", "create", "--label", LABEL, self.volume], capture_output=True, text=True, timeout=60
        )
        if made.returncode != 0:
            raise SandboxError(f"Docker couldn't make the build's volume: {made.stderr.strip()[-200:]}")
        results: list[StepResult] = []
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
            failed = False
            for i, step in enumerate(steps):
                left = deadline - time.monotonic()
                if failed or self.cancelled or left <= 1:
                    results.append(StepResult(step.name, step.label, None, 0.0, skipped=True,
                                              output="" if failed or self.cancelled else "The build ran out of time before this step."))
                    failed = True
                    continue
                cap = min(left, step.timeout) if step.timeout else left
                name = f"{self.volume}-{i}"
                if on_step is not None:
                    on_step(step)
                began = time.monotonic()
                code, out, late = self._exec(name, self._args(name, step.command, network=step.network, env=step.env), cap)
                took = time.monotonic() - began
                if self.cancelled and code != 0:
                    results.append(StepResult(step.name, step.label, None, took, out, skipped=True))
                    failed = True
                    continue
                result = StepResult(step.name, step.label, code, took, out, timed_out=late)
                results.append(result)
                failed = not result.ok
            return results
        finally:
            try:
                subprocess.run([self.cli, "volume", "rm", "-f", self.volume], capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError):
                pass
