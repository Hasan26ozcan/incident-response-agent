"""Isolation backends for the sandbox (Stage 16).

Three backends, honest about what each guarantees (``Backend.guarantees``). The runner refuses a command whose
requirements a backend does not claim, so a weak backend can still run read-only commands but never a mutating one.

* ``process``   - a plain child process: scrubbed environment, rlimits, timeout, output cap, process-group kill.
                  No network or file-system isolation. Works everywhere (also Windows, with fewer limits).
* ``namespace`` - Linux only: ``unshare`` user + net + pid + mount + ipc + uts namespaces, a tmpfs root holding only a
                  read-only copy of the Python runtime, all capabilities dropped, ``no_new_privs``.
* ``docker``    - ``docker run --network none --read-only --cap-drop ALL ...`` (best isolation, needs Docker).

None of these is a hardened multi-tenant boundary: the kernel is shared. They are a second line behind the allow-list.
"""

from __future__ import annotations

import contextlib
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Protocol

NETWORK, FILESYSTEM, PROCESSES, ENV, TIME, OUTPUT, CAPS, MEMORY = (
    "network",
    "filesystem",
    "processes",
    "env",
    "time",
    "output",
    "caps",
    "memory",
)
MB = 1024 * 1024
DEFAULT_IMAGE = "python:3.13-slim"


@dataclass(frozen=True)
class Limits:
    timeout: float = 10.0
    cpu_seconds: int = 10
    memory_bytes: int = 768 * MB
    max_output: int = 64_000
    max_file_bytes: int = 8 * MB
    max_open_files: int = 128
    tmp_bytes: int = 16 * MB
    max_pids: int = 64  # enforced by docker only (a pid namespace plus the timeout bound the other backends)

    def __post_init__(self) -> None:
        names = (
            "timeout",
            "cpu_seconds",
            "memory_bytes",
            "max_output",
            "max_file_bytes",
            "max_open_files",
            "tmp_bytes",
        )
        for name in names:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_pids < 1:
            raise ValueError("max_pids must be at least 1")


@dataclass(frozen=True)
class ExecSpec:
    code: str  # Python source, run as ``python -I -S -B -c <code>``
    stdin: bytes = b""
    limits: Limits = field(default_factory=Limits)


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None  # None when the process had to be killed
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_truncated: bool = False
    killed: str | None = None  # timeout | output-limit | unavailable
    duration: float = 0.0
    backend: str = ""


class Backend(Protocol):
    name: str
    guarantees: frozenset[str]

    def available(self) -> tuple[bool, str]: ...

    def run(self, spec: ExecSpec) -> ExecResult: ...


# --- shared process plumbing ---------------------------------------------------------------------------------------


def minimal_env() -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    if os.name == "nt":  # pragma: no cover - Windows only
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


def _limits_preexec(lim: Limits) -> Callable[[], None]:
    def apply() -> None:  # pragma: no cover - runs in the child between fork and exec
        import resource

        pairs = (
            (resource.RLIMIT_CPU, lim.cpu_seconds),  # type: ignore[attr-defined]
            (resource.RLIMIT_AS, lim.memory_bytes),  # type: ignore[attr-defined]
            (resource.RLIMIT_FSIZE, lim.max_file_bytes),  # type: ignore[attr-defined]
            (resource.RLIMIT_NOFILE, lim.max_open_files),  # type: ignore[attr-defined]
            (resource.RLIMIT_CORE, 0),  # type: ignore[attr-defined]
        )
        for which, value in pairs:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(which, (value, value))  # type: ignore[attr-defined]

    return apply


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":  # pragma: no cover - Windows only
        taskkill = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
        subprocess.run([taskkill, "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)  # noqa: S603
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)  # type: ignore[attr-defined]
    with contextlib.suppress(ProcessLookupError, PermissionError):
        proc.kill()


class _Capture:
    """Reads one pipe into a bounded buffer; keeps draining so the writer never blocks, flags overflow."""

    def __init__(self, stream: IO[bytes], cap: int, overflow: threading.Event) -> None:
        self.buf = bytearray()
        self.cap = cap
        self.truncated = False
        self._overflow = overflow
        self._stream = stream
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        fd = self._stream.fileno()
        try:
            while True:
                ready, _, _ = select.select([fd], [], [], 0.2) if os.name != "nt" else ([fd], [], [])
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    return
                room = self.cap - len(self.buf)
                if room > 0:
                    self.buf += chunk[:room]
                if len(chunk) > room:
                    self.truncated = True
                    self._overflow.set()
        except (OSError, ValueError):
            return


def _feed(proc: subprocess.Popen[bytes], data: bytes) -> None:
    try:
        if proc.stdin:
            proc.stdin.write(data)
            proc.stdin.close()
    except (OSError, ValueError):
        pass


def _popen(argv: list[str], lim: Limits, env: dict[str, str], cwd: str) -> subprocess.Popen[bytes]:
    if os.name == "nt":  # pragma: no cover - Windows only
        return subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    return subprocess.Popen(  # noqa: S603
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        close_fds=True,
        start_new_session=True,
        preexec_fn=_limits_preexec(lim),  # noqa: PLW1509
    )


def run_process(
    argv: list[str],
    spec: ExecSpec,
    name: str,
    env: dict[str, str],
    cwd: str,
    on_kill: Callable[[], None] | None = None,
) -> ExecResult:
    lim = spec.limits
    start = time.monotonic()
    try:
        proc = _popen(argv, lim, env, cwd)
    except OSError as exc:
        return ExecResult(None, b"", f"cannot start: {type(exc).__name__}".encode(), killed="unavailable", backend=name)
    overflow = threading.Event()
    assert proc.stdout is not None and proc.stderr is not None
    out = _Capture(proc.stdout, lim.max_output, overflow)
    err = _Capture(proc.stderr, lim.max_output, overflow)
    threading.Thread(target=_feed, args=(proc, spec.stdin), daemon=True).start()
    killed: str | None = None
    deadline = start + lim.timeout
    while proc.poll() is None:
        if overflow.is_set():
            killed = "output-limit"
            break
        if time.monotonic() >= deadline:
            killed = "timeout"
            break
        overflow.wait(0.02)
    if killed and on_kill:
        with contextlib.suppress(Exception):
            on_kill()
    _kill_tree(proc)  # also reaps same-group stragglers after a normal exit
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
    for cap in (out, err):
        cap.thread.join(timeout=2)
    for pipe in (proc.stdout, proc.stderr, proc.stdin):
        if pipe:
            with contextlib.suppress(OSError, ValueError):
                pipe.close()
    return ExecResult(
        None if killed else proc.returncode,
        bytes(out.buf),
        bytes(err.buf),
        timed_out=killed == "timeout",
        output_truncated=out.truncated or err.truncated,
        killed=killed,
        duration=time.monotonic() - start,
        backend=name,
    )


def _python_argv(python: str, code: str) -> list[str]:
    return [python, "-I", "-S", "-B", "-c", code]


# --- process ---------------------------------------------------------------------------------------------------------


class ProcessBackend:
    name = "process"
    guarantees = frozenset({ENV, TIME, OUTPUT} | ({MEMORY} if os.name != "nt" else set()))

    def available(self) -> tuple[bool, str]:
        return True, "always available (weak isolation: no network or file-system isolation)"

    def run(self, spec: ExecSpec) -> ExecResult:
        scratch = tempfile.mkdtemp(prefix="sbx-")
        try:
            return run_process(_python_argv(sys.executable, spec.code), spec, self.name, minimal_env(), scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# --- namespace (Linux) -----------------------------------------------------------------------------------------------

_DETACH_OLD_ROOT = "import ctypes; ctypes.CDLL(None, use_errno=True).umount2(b'/old', 2)"


def _real_python() -> str:
    return os.path.realpath(sys.executable)


def _contained(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _bind_dirs() -> list[str]:
    """Directories the sandboxed Python needs (its prefix and the system libraries), without nested duplicates."""
    cand = [os.path.realpath(sys.base_prefix)]
    for d in ("/lib", "/lib64", "/usr/lib", "/usr/lib64"):
        if os.path.isdir(d) and not os.path.islink(d):
            cand.append(d)
    keep: list[str] = []
    for d in sorted(set(cand), key=len):
        if not any(_contained(d, k) for k in keep):
            keep.append(d)
    return keep


def _top_level_links() -> list[tuple[str, str]]:
    """Merged-/usr symlinks (``/lib64 -> usr/lib64``) that the new root has to recreate."""
    links = []
    for name in ("bin", "sbin", "lib", "lib32", "lib64"):
        path = "/" + name
        if os.path.islink(path):
            links.append((name, os.readlink(path)))
    return links


def namespace_script(binds: list[str], setpriv: str, python: str, lim: Limits, links: list[tuple[str, str]]) -> str:
    """The ``sh`` script that builds the new root and then execs the sandboxed program (pure; unit-tested)."""
    q = shlex.quote
    lines = [
        "set -e",
        "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
        'root="$1"; shift',
        f'mount -t tmpfs -o size={lim.tmp_bytes // MB + 8}m,mode=755 tmpfs "$root"',
        'mkdir "$root/tmp" "$root/dev" "$root/old"',
        f'mount -t tmpfs -o size={max(lim.tmp_bytes // MB, 1)}m,mode=1777,noexec,nosuid,nodev tmpfs "$root/tmp"',
    ]
    for d in binds:
        lines.append(f'mkdir -p "$root"{q(d)}')
        lines.append(f'mount --bind {q(d)} "$root"{q(d)}')
        lines.append(f'mount -o remount,bind,ro,nosuid,nodev "$root"{q(d)}')
    for name, target in links:
        lines.append(f'ln -s {q(target)} "$root/{name}"')
    if not any(_contained(setpriv, d) for d in binds):
        lines.append(f'mkdir -p "$root"{q(os.path.dirname(setpriv))}; : > "$root"{q(setpriv)}')
        lines.append(f'mount --bind {q(setpriv)} "$root"{q(setpriv)}')
    for dev in ("null", "zero", "urandom"):
        lines.append(f'echo -n > "$root/dev/{dev}"; mount --bind /dev/{dev} "$root/dev/{dev}"')
    lines += [
        'mount -o remount,ro "$root"',
        'cd "$root"',
        "pivot_root . old",
        "cd /",
        f"{q(python)} -I -S -c {q(_DETACH_OLD_ROOT)}",
        "cd /tmp",
        "unset OLDPWD PWD",
        f'exec {q(setpriv)} --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs "$@"',
    ]
    return "\n".join(lines)


class NamespaceBackend:
    name = "namespace"
    guarantees = frozenset({ENV, TIME, OUTPUT, NETWORK, FILESYSTEM, PROCESSES, CAPS, MEMORY})

    def __init__(self) -> None:
        self._probe: tuple[bool, str] | None = None

    def _command(self, spec: ExecSpec, root: str) -> list[str]:
        setpriv = shutil.which("setpriv") or "/usr/bin/setpriv"
        python = _real_python()
        script = namespace_script(_bind_dirs(), setpriv, python, spec.limits, _top_level_links())
        return [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--pid",
            "--fork",
            "--kill-child",
            "--mount",
            "--ipc",
            "--uts",
            "--",
            "sh",
            "-c",
            script,
            "sandbox",
            root,
            *_python_argv(python, spec.code),
        ]

    def available(self) -> tuple[bool, str]:
        if self._probe is not None:
            return self._probe
        tools = ("unshare", "setpriv")
        if not sys.platform.startswith("linux"):
            self._probe = (False, "needs Linux")
        elif not all(shutil.which(t) for t in tools) or not shutil.which(
            "pivot_root", path="/usr/sbin:/sbin:/usr/bin:/bin"
        ):
            self._probe = (False, "needs unshare, setpriv and pivot_root (util-linux)")
        else:
            res = self.run(ExecSpec("print(6 * 7)", b"", Limits(timeout=10)))
            ok = res.exit_code == 0 and res.stdout.strip() == b"42"
            self._probe = (True, "ok (probe ran)") if ok else (False, "user namespaces are not permitted here")
        return self._probe

    def run(self, spec: ExecSpec) -> ExecResult:
        root = tempfile.mkdtemp(prefix="sbx-root-")
        try:
            return run_process(self._command(spec, root), spec, self.name, minimal_env(), tempfile.gettempdir())
        finally:
            with contextlib.suppress(OSError):
                os.rmdir(root)


# --- docker ----------------------------------------------------------------------------------------------------------


def docker_run_argv(docker: str, container: str, image: str, spec: ExecSpec) -> list[str]:
    """The exact ``docker run`` command line (pure, so tests can assert every isolation flag)."""
    lim = spec.limits
    mem = f"{max(lim.memory_bytes // MB, 16)}m"
    tmpfs = f"/tmp:rw,noexec,nosuid,nodev,size={max(lim.tmp_bytes // MB, 1)}m"  # nosec B108
    return [
        docker,
        "run",
        "--rm",
        "-i",
        "--name",
        container,
        "--network",
        "none",
        "--ipc",
        "none",
        "--read-only",
        "--tmpfs",
        tmpfs,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(lim.max_pids),
        "--memory",
        mem,
        "--memory-swap",
        mem,
        "--cpus",
        "1",
        "--ulimit",
        f"cpu={lim.cpu_seconds}:{lim.cpu_seconds}",
        "--ulimit",
        f"fsize={lim.max_file_bytes}:{lim.max_file_bytes}",
        "--ulimit",
        f"nofile={lim.max_open_files}:{lim.max_open_files}",
        "--ulimit",
        "core=0:0",
        "--user",
        "65534:65534",
        "--workdir",
        "/tmp",  # nosec B108
        "--pull",
        "never",
        image,
        "python",
        "-I",
        "-S",
        "-B",
        "-c",
        spec.code,
    ]


class DockerBackend:
    name = "docker"
    guarantees = frozenset({ENV, TIME, OUTPUT, NETWORK, FILESYSTEM, PROCESSES, CAPS, MEMORY})

    def __init__(self, docker: str | None = None, image: str | None = None) -> None:
        self._docker = docker or shutil.which("docker")
        self._image = image or os.environ.get("SANDBOX_DOCKER_IMAGE") or DEFAULT_IMAGE
        self._probe: tuple[bool, str] | None = None

    def available(self) -> tuple[bool, str]:
        if self._probe is not None:
            return self._probe
        if not self._docker:
            self._probe = (False, "docker is not installed")
            return self._probe
        try:
            info = subprocess.run([self._docker, "info"], capture_output=True, timeout=15, check=False)  # noqa: S603
            if info.returncode != 0:
                self._probe = (False, "the docker daemon is not reachable")
                return self._probe
            img = subprocess.run(  # noqa: S603
                [self._docker, "image", "inspect", self._image], capture_output=True, timeout=15, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._probe = (False, f"docker did not answer: {type(exc).__name__}")
            return self._probe
        if img.returncode != 0:
            self._probe = (False, f"image {self._image} is not present; run: docker pull {self._image}")
        else:
            self._probe = (True, f"ok (image {self._image})")
        return self._probe

    def run(self, spec: ExecSpec) -> ExecResult:
        if not self._docker:
            return ExecResult(None, b"", b"docker missing", killed="unavailable", backend=self.name)
        container = f"sbx-{uuid.uuid4().hex[:12]}"
        argv = docker_run_argv(self._docker, container, self._image, spec)
        docker = self._docker

        def cleanup() -> None:
            subprocess.run([docker, "rm", "-f", container], capture_output=True, timeout=20, check=False)  # noqa: S603

        try:
            return run_process(argv, spec, self.name, _docker_env(), tempfile.gettempdir(), on_kill=cleanup)
        finally:
            with contextlib.suppress(Exception):
                cleanup()


def _docker_env() -> dict[str, str]:
    """The docker CLI needs a few variables to find its daemon; nothing else is passed on."""
    keep = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "SYSTEMROOT", "USERPROFILE", "APPDATA")
    return {k: os.environ[k] for k in keep if k in os.environ}


# --- selection -------------------------------------------------------------------------------------------------------


def all_backends() -> dict[str, Backend]:
    return {"docker": DockerBackend(), "namespace": NamespaceBackend(), "process": ProcessBackend()}


def select_backend(preferred: str | None = None, backends: dict[str, Backend] | None = None) -> Backend:
    """Pick the requested backend (must be available) or the strongest one: docker > namespace > process."""
    pool = backends if backends is not None else all_backends()
    want = preferred or os.environ.get("SANDBOX_BACKEND") or None
    if want:
        if want not in pool:
            raise RuntimeError(f"unknown sandbox backend {want!r}; choose from {', '.join(pool)}")
        ok, reason = pool[want].available()
        if not ok:
            raise RuntimeError(f"sandbox backend {want!r} is not available: {reason}")
        return pool[want]
    for name in ("docker", "namespace", "process"):
        if name in pool and pool[name].available()[0]:
            return pool[name]
    raise RuntimeError("no sandbox backend is available")


def describe_backends(backends: dict[str, Backend] | None = None) -> list[str]:
    rows = []
    for name, b in (backends if backends is not None else all_backends()).items():
        ok, reason = b.available()
        state = "available" if ok else "unavailable"
        rows.append(f"{name:<10} {state:<12} {reason}; guarantees: {', '.join(sorted(b.guarantees))}")
    return rows
