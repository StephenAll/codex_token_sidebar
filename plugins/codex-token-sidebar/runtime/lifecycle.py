"""Local single-instance ownership and bounded status/stop control (standard library)."""

from __future__ import annotations

import fcntl
import argparse
import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from copy import deepcopy

from startup import build_identity, legacy_instances, log_event
from health import health_view


PROTOCOL = 1
MAX_MESSAGE = 4096
REQUEST_TIMEOUT = 0.5
CONTROL_TIMEOUT = 5.0
STARTUP_TIMEOUT = 8.0
OPERATION_TIMEOUT = 20.0


class LifecycleError(RuntimeError):
    pass


class InstanceBusy(LifecycleError):
    pass


def state_directory() -> Path:
    override = os.environ.get("CODEX_TOKEN_SIDEBAR_STATE_DIR")
    return Path(override) if override else Path.home() / "Library/Application Support/Codex Token Sidebar"


class StateDirectory:
    """Keep a verified directory descriptor for state-file operations."""

    def __init__(self, path: Path):
        self.path = Path(os.path.abspath(path))
        self.socket_path = str(self.path / "control.sock")
        if len(os.fsencode(self.socket_path)) >= 104:
            raise LifecycleError("Control socket path is too long")
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(self.fd).st_uid != os.getuid():
                raise LifecycleError("State directory must belong to the current user")
            os.fchmod(self.fd, 0o700)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def inspect(self, name: str):
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def validate(self, name: str, socket_file: bool = False):
        info = self.inspect(name)
        if info is not None:
            valid_type = stat.S_ISSOCK(info.st_mode) if socket_file else stat.S_ISREG(info.st_mode)
            if not valid_type or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise LifecycleError(f"Unsafe state file: {name}")
        return info

    def acquire(self, name="instance.lock") -> int:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise LifecycleError("Unsafe instance lock")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstanceBusy("Sidebar instance is busy") from exc
            return fd
        except BaseException:
            os.close(fd)
            raise

    def reclaim(self):
        # Only the owner of instance.lock may call this. Never unlink the lock itself.
        for name, is_socket in (("control.sock", True), ("sidebar.pid", False)):
            if self.validate(name, is_socket) is not None:
                os.unlink(name, dir_fd=self.fd)


def _receive(connection: socket.socket) -> dict:
    deadline = time.monotonic() + REQUEST_TIMEOUT
    data = bytearray()
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LifecycleError("Control request timed out")
        connection.settimeout(remaining)
        chunk = connection.recv(min(1024, MAX_MESSAGE + 1 - len(data)))
        if not chunk:
            raise LifecycleError("Incomplete control message")
        data.extend(chunk)
        if len(data) > MAX_MESSAGE:
            raise LifecycleError("Control message is too large")
    if data.count(b"\n") != 1 or not data.endswith(b"\n"):
        raise LifecycleError("Expected one control message")
    try:
        value = json.loads(data)
    except RecursionError as exc:
        raise LifecycleError("Control message nesting is too deep") from exc
    if not isinstance(value, dict):
        raise LifecycleError("Expected a control object")
    return value


def _send(connection: socket.socket, value: dict):
    data = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    if len(data) > MAX_MESSAGE:
        raise LifecycleError("Control message is too large")
    connection.settimeout(REQUEST_TIMEOUT)
    connection.sendall(data)


def request(state: StateDirectory, action: str, instance_id: str | None = None) -> dict:
    info = state.validate("control.sock", socket_file=True)
    if info is None:
        raise LifecycleError("Control endpoint is not ready")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise LifecycleError("Control endpoint permissions must be 0600")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(REQUEST_TIMEOUT)
        connection.connect(state.socket_path)
        _send(connection, {"protocol": PROTOCOL, "action": action, "instanceId": instance_id})
        response = _receive(connection)
    if (type(response.get("protocol")) is not int or response["protocol"] != PROTOCOL
            or not isinstance(response.get("instanceId"), str)
            or response.get("status") not in ("starting", "running", "stopping", "stale")):
        raise LifecycleError("Invalid control response")
    if instance_id is not None and response["instanceId"] != instance_id:
        raise LifecycleError("Sidebar instance changed")
    return response


def status(state: StateDirectory) -> dict:
    try:
        fd = state.acquire()
    except InstanceBusy:
        return request(state, "status")
    else:
        os.close(fd)
        return {"protocol": PROTOCOL, "status": "stopped"}


class Instance:
    """Runtime owns the lock; the control thread never calls reader or CDP."""

    def __init__(self, directory: Path, inherited_fd: int | None = None, *, identity: dict | None = None):
        self.state = StateDirectory(directory)
        self.fd = None
        self.listener = None
        self.thread = None
        self.owned_files = {}
        self.stop_event = threading.Event()
        self.closed = threading.Event()
        self.instance_id = str(uuid.uuid4())
        self.snapshot_lock = threading.Lock()
        self.details = {**(identity or {}), "startedAt": time.time(), "ready": False}
        self.health_deadline = time.monotonic() + STARTUP_TIMEOUT
        try:
            if inherited_fd is None:
                self.fd = self.state.acquire()
            else:
                info = os.fstat(inherited_fd)
                expected = self.state.validate("instance.lock")
                if expected is None or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
                    raise LifecycleError("Invalid inherited instance lock")
                self.fd = inherited_fd
                os.set_inheritable(self.fd, False)
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.state.reclaim()
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(self.state.socket_path)
            os.chmod("control.sock", 0o600, dir_fd=self.state.fd, follow_symlinks=False)
            self.owned_files["control.sock"] = self.state.inspect("control.sock")
            self.listener.listen(32)
            self.listener.settimeout(0.1)
            pid_fd = os.open("sidebar.pid", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.state.fd)
            with os.fdopen(pid_fd, "w") as handle:
                handle.write(str(os.getpid()) + "\n")
            self.owned_files["sidebar.pid"] = self.state.inspect("sidebar.pid")
            self.thread = threading.Thread(target=self._serve, name="sidebar-control", daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def _serve(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    value = _receive(connection)
                    if (type(value.get("protocol")) is not int or value["protocol"] != PROTOCOL
                            or value.get("action") not in ("status", "stop")):
                        raise LifecycleError("Unsupported control request")
                    with self.snapshot_lock:
                        details = deepcopy(self.details)
                        if "health" in details:
                            details["health"] = health_view(details["health"], self.health_deadline, time.monotonic())
                    result = "stopping" if self.stop_event.is_set() else ("running" if details["ready"] else "starting")
                    if value["action"] == "stop":
                        if value.get("instanceId") != self.instance_id:
                            result = "stale"
                        else:
                            self.stop_event.set()
                            result = "stopping"
                    _send(connection, {**details, "protocol": PROTOCOL, "instanceId": self.instance_id,
                                       "pid": os.getpid(), "status": result})
                except (OSError, ValueError, LifecycleError):
                    # Malformed/slow clients do not take down the control service.
                    try:
                        _send(connection, {"protocol": PROTOCOL, "status": "error"})
                    except (OSError, LifecycleError):
                        pass

    def publish_health(self, snapshot, deadline):
        with self.snapshot_lock:
            self.details["health"] = deepcopy(snapshot)
            self.health_deadline = deadline

    def mark_ready(self):
        with self.snapshot_lock:
            if self.stop_event.is_set():
                raise LifecycleError("Initialization was cancelled")
            self.details["ready"] = True

    def close(self):
        self.closed.set()
        if self.listener is not None:
            self.listener.close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.fd is not None:
            # Keep ownership until cleanup completes, and compare inode identities.
            for name, owned in self.owned_files.items():
                current = self.state.inspect(name)
                if current and (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                    os.unlink(name, dir_fd=self.state.fd)
            os.close(self.fd)
            self.fd = None
        self.state.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _operation_lock(state: StateDirectory, deadline: float) -> int:
    while time.monotonic() < deadline:
        try:
            return state.acquire("launch.lock")
        except InstanceBusy:
            time.sleep(.025)
    raise LifecycleError("Another start/stop operation did not complete before the deadline")


def _matches(result: dict, expected: dict | None) -> bool:
    return expected is None or all(result.get(key) == expected[key] for key in ("version", "fingerprint"))


def start(state: StateDirectory, command: list[str], *, expected: dict | None = None) -> dict:
    deadline = time.monotonic() + OPERATION_TIMEOUT
    operation_fd = _operation_lock(state, deadline)
    attempt = str(uuid.uuid4())
    try:
        return _start_locked(state, command, expected, deadline, attempt)
    except (OSError, ValueError, LifecycleError) as exc:
        try:
            log_event(state.path, attempt, "launch_failed", exception=type(exc).__name__,
                      reason=str(exc) if isinstance(exc, LifecycleError) else "process_or_log_io")
        except (OSError, ValueError):
            pass
        raise
    finally:
        os.close(operation_fd)


def _start_locked(state: StateDirectory, command: list[str], expected: dict | None,
                  deadline: float, attempt: str) -> dict:
    child = None
    initialization_deadline = deadline
    try:
        while time.monotonic() < min(deadline, initialization_deadline):
            try:
                fd = state.acquire()
            except InstanceBusy:
                try:
                    result = request(state, "status")
                except (OSError, ValueError, LifecycleError):
                    result = None
                if result is not None and result["status"] == "running" and result.get("ready") is True:
                    if _matches(result, expected):
                        return result
                    if child is not None:
                        raise LifecycleError("Source changed during initialization; retry startup")
                    _stop_instance(state, result, deadline)
                elif result is not None and child is None and result["status"] == "running":
                    # H1 endpoints have an instance identity but no readiness metadata.
                    _stop_instance(state, result, deadline)
            else:
                try:
                    if child is not None:
                        raise LifecycleError("Sidebar initialization failed; see startup.log")
                    runtime = command[1]
                    try:
                        default = Path.home() / "Library/Application Support/Codex Token Sidebar"
                        legacy = legacy_instances(Path(runtime), state.path == default)
                    except (OSError, subprocess.SubprocessError) as exc:
                        raise LifecycleError("Could not verify legacy processes; startup cancelled") from exc
                    if legacy:
                        raise LifecycleError("Legacy runtime requires explicit migration before startup; PIDs: "
                                             + ",".join(map(str, legacy)))
                    log_event(state.path, attempt, "launch")
                    bootstrap = str(Path(runtime).with_name("startup.py"))
                    child = subprocess.Popen([command[0], "-B", bootstrap, attempt, str(state.path), runtime,
                                              *command[2:], "--lifecycle-lock-fd", str(fd)],
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, pass_fds=(fd,), start_new_session=True)
                    initialization_deadline = min(deadline, time.monotonic() + STARTUP_TIMEOUT)
                finally:
                    # Do not explicitly unlock the open-file description inherited by the child.
                    os.close(fd)
            if child is not None and child.poll() is not None:
                raise LifecycleError("Sidebar initialization failed; see startup.log")
            time.sleep(.025)
        raise LifecycleError("Sidebar initialization timed out or existing instance is unresponsive; see startup.log")
    except BaseException:
        # Only our fresh child may be terminated after failed initialization.
        # Existing instances are stopped exclusively through their bound endpoint.
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        raise


def stop(state: StateDirectory) -> dict:
    deadline = time.monotonic() + OPERATION_TIMEOUT
    operation_fd = _operation_lock(state, deadline)
    try:
        return _stop_instance(state, status(state), deadline)
    finally:
        os.close(operation_fd)


def _stop_instance(state: StateDirectory, current: dict, operation_deadline: float) -> dict:
    if current["status"] == "stopped":
        return current
    identity = current["instanceId"]
    reply = request(state, "stop", identity)
    if reply["status"] != "stopping":
        raise LifecycleError("Sidebar refused the stop request")
    deadline = min(operation_deadline, time.monotonic() + CONTROL_TIMEOUT)
    while time.monotonic() < deadline:
        try:
            current = status(state)
            if current["status"] == "stopped" or current.get("instanceId") != identity:
                return {"protocol": PROTOCOL, "status": "stopped", "instanceId": identity}
        except (OSError, ValueError, LifecycleError):
            pass
        time.sleep(.025)
    raise LifecycleError("Sidebar stop is pending; no signal was sent")


def validate_start_options(args: list[str]):
    class Options(argparse.ArgumentParser):
        def error(self, message):
            # Do not persist arbitrary unrecognized arguments in startup diagnostics.
            raise LifecycleError("Invalid background runtime options; use the foreground --help")

    parser = Options(add_help=False)
    parser.add_argument("--port", type=int)
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--log-file")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--quiet", action="store_true")
    value = parser.parse_args(args)
    if not math.isfinite(value.interval) or (value.port is not None and not 1 <= value.port <= 65535):
        raise LifecycleError("Invalid port or polling interval")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"start", "stop", "status"}:
        print("Usage: lifecycle.py start [runtime options] | status [--json] | stop [--json]", file=sys.stderr)
        return 2
    action, *args = argv
    state = None
    try:
        state = StateDirectory(state_directory())
        if action == "start":
            validate_start_options(args)
            runtime = Path(__file__).with_name("codex_token_sidebar.py")
            expected = build_identity(runtime)
            result = start(state, [sys.executable, str(runtime),
                                   "--log-file", str(state.path / "sidebar.log"), *args, "--quiet"], expected=expected)
        else:
            if args not in ([], ["--json"]):
                raise LifecycleError("Expected only --json")
            result = stop(state) if action == "stop" else status(state)
        print(json.dumps(result) if args == ["--json"] else f"Codex Token Sidebar is {result['status']}"
              + (f" (pid {result['pid']})" if "pid" in result else ""))
        return 0
    except (OSError, ValueError, LifecycleError) as exc:
        if state is not None:
            try:
                log_event(state.path, "preflight", "command_failed", exception=type(exc).__name__)
            except (OSError, ValueError):
                pass
        print(f"Codex Token Sidebar: {exc}", file=sys.stderr)
        return 2
    finally:
        if state is not None:
            state.close()


if __name__ == "__main__":
    raise SystemExit(main())
