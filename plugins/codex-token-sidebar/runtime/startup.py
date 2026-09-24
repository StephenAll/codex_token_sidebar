"""Build identity and bounded pre-initialization diagnostics for the launcher."""

from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    print("Codex Token Sidebar requires Python 3.10 or later", file=sys.stderr)
    raise SystemExit(2)

import fcntl
import io
import json
import os
from pathlib import Path
import runpy
import re
import subprocess
import stat
import time
import traceback

from identity import build_identity


LOG_BYTES = 64 * 1024
LOG_BACKUPS = 2


def legacy_instances(runtime_path: Path, include_installed: bool = False) -> list[int]:
    """Conservatively report direct legacy daemons; never signal any result."""
    result = subprocess.run(["ps", "-U", str(os.getuid()), "-o", "pid=,command="],
                            capture_output=True, text=True, timeout=2, check=True)
    runtime = re.escape(str(runtime_path.resolve()))
    patterns = [runtime]
    if include_installed:
        cache = re.escape(str(Path.home() / ".codex/plugins/cache"))
        patterns.append(cache + r"/[^/\s]+/codex-token-sidebar/[^/\s]+/runtime/codex_token_sidebar\.py")
    found = []
    for row in result.stdout.splitlines():
        parts = row.strip().split(None, 1)
        if len(parts) != 2:
            continue
        pid, command = parts
        executable = command.split(None, 1)[0]
        if not re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", Path(executable).name, re.I):
            continue
        if "/runtime/startup.py" in command or "--lifecycle-lock-fd" in command:
            continue
        if re.search(r"(?:^|\s)--(?:once|self-test|help)(?:\s|$)", command):
            continue
        if any(re.search(r"\s" + pattern + r"(?:\s|$)", command) for pattern in patterns):
            found.append(int(pid))
    return found


def _validate(directory_fd: int, name: str):
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("Unsafe startup log file")


def _open_private(directory_fd: int, name: str, flags: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise OSError("Unsafe startup log descriptor")
    os.fchmod(fd, 0o600)
    return fd


def log_event(directory: Path, attempt: str, event: str, **fields):
    """One bounded append under a separate lock; no open file survives rotation."""
    record = {"time": time.time(), "attempt": attempt, "event": event,
              **{key: str(value)[:2048] for key, value in fields.items()}}
    data = json.dumps(record, separators=(",", ":")).encode() + b"\n"
    if len(data) > 8192:
        raise ValueError("Startup log record too large")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        info = os.fstat(directory_fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("Startup log requires a private state directory")
        lock_fd = _open_private(directory_fd, "startup-log.lock", os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + .5
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("Startup log lock timed out")
                time.sleep(.01)
        for name in ["startup.log", "startup.log.1", "startup.log.2"]:
            _validate(directory_fd, name)
        fd = _open_private(directory_fd, "startup.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            if os.fstat(fd).st_size + len(data) > LOG_BYTES:
                os.close(fd)
                fd = None
                for index in range(LOG_BACKUPS, 0, -1):
                    previous = "startup.log" if index == 1 else f"startup.log.{index-1}"
                    try:
                        os.replace(previous, f"startup.log.{index}", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                fd = _open_private(directory_fd, "startup.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
            os.write(fd, data)
        finally:
            if fd is not None:
                os.close(fd)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)


class StartupStderr(io.TextIOBase):
    def __init__(self, directory: Path, attempt: str):
        self.directory = directory
        self.attempt = attempt

    def write(self, value):
        if value.strip():
            log_event(self.directory, self.attempt, "stderr", text=value[:2048])
        return len(value)

    def flush(self):
        pass


def bootstrap(argv: list[str]) -> int:
    attempt, directory, runtime, *args = argv
    state = Path(directory)
    previous = sys.stderr
    try:
        sys.stderr = StartupStderr(state, attempt)
        log_event(state, attempt, "bootstrap")
        sys.argv = [runtime, *args]
        sys.path.insert(0, str(Path(runtime).parent))
        runpy.run_path(runtime, run_name="__main__")
        return 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        if code:
            log_event(state, attempt, "exit", code=code)
        return code
    except Exception as exc:
        # Exception messages can contain user content; retain type and frame locations.
        frames = traceback.extract_tb(exc.__traceback__)
        location = ";".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames[-5:])
        try:
            log_event(state, attempt, "initialization_failed", exception=type(exc).__name__, location=location)
        except (OSError, ValueError):
            pass
        return 2
    finally:
        sys.stderr = previous


if __name__ == "__main__":
    raise SystemExit(0 if sys.argv[1:] == ["--check-python"] else bootstrap(sys.argv[1:]))
