"""Windows-native single-instance launcher and loopback control for the sidebar.

The lock and control endpoint have separate owners: the daemon holds the lifetime
lock, while each start/stop operation holds a short-lived launch lock. A private
random token authenticates control requests sent over the loopback endpoint.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

if sys.version_info < (3, 10):
    raise SystemExit("Codex Token Sidebar requires Python 3.10 or later")

if os.name == "nt":
    import msvcrt
else:  # Exercise the control protocol in non-Windows CI.
    import fcntl

from health import health_view
from identity import build_identity


PROTOCOL = 1
MAX_MESSAGE = 4096
REQUEST_TIMEOUT = 0.5
OPERATION_TIMEOUT = 20.0
STARTUP_TIMEOUT = 8.0
CONTROL_TIMEOUT = 5.0
INSTANCE_LOCK_RETRY = 1.0
_OWNED_CHILDREN: dict[int, subprocess.Popen] = {}


class LifecycleError(RuntimeError):
    pass


class InstanceBusy(LifecycleError):
    pass


def state_directory() -> Path:
    override = os.environ.get("CODEX_TOKEN_SIDEBAR_STATE_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "Codex Token Sidebar"


class StateDirectory:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().absolute()
        self.path.mkdir(parents=True, exist_ok=True)
        self.endpoint_path = self.path / "control.json"


class FileLock:
    """Lock the first byte; the open handle keeps the lock for its full lifetime."""

    def __init__(self, path: Path):
        self.handle = open(path, "a+b", buffering=0)
        try:
            if self.handle.seek(0, os.SEEK_END) == 0:
                self.handle.write(b"0")
            self.handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise InstanceBusy("Sidebar instance is busy") from exc

    def close(self):
        if self.handle.closed:
            return
        self.handle.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _read_endpoint(state: StateDirectory) -> dict:
    try:
        info = state.endpoint_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MESSAGE:
            raise LifecycleError("Control endpoint is invalid")
        value = json.loads(state.endpoint_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LifecycleError("Control endpoint is not ready") from exc
    except (OSError, ValueError) as exc:
        raise LifecycleError("Control endpoint is invalid") from exc
    if (not isinstance(value, dict) or value.get("protocol") != PROTOCOL
            or not isinstance(value.get("instanceId"), str)
            or not isinstance(value.get("token"), str) or len(value["token"]) != 64
            or type(value.get("port")) is not int or not 1 <= value["port"] <= 65535):
        raise LifecycleError("Control endpoint is invalid")
    return value


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
            raise LifecycleError("Incomplete control response")
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
        raise LifecycleError("Invalid control message")
    return value


def _send(connection: socket.socket, value: dict):
    encoded = json.dumps(value, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > MAX_MESSAGE:
        raise LifecycleError("Control message is too large")
    connection.settimeout(REQUEST_TIMEOUT)
    connection.sendall(encoded)


def request(state: StateDirectory, action: str, instance_id: str | None = None) -> dict:
    endpoint = _read_endpoint(state)
    try:
        with socket.create_connection(("127.0.0.1", endpoint["port"]), REQUEST_TIMEOUT) as connection:
            _send(connection, {"protocol": PROTOCOL, "action": action,
                               "instanceId": instance_id, "token": endpoint["token"]})
            result = _receive(connection)
    except (OSError, ValueError) as exc:
        raise LifecycleError("Control endpoint did not respond") from exc
    if (result.get("protocol") != PROTOCOL or result.get("instanceId") != endpoint["instanceId"]
            or result.get("status") not in ("starting", "running", "stopping", "stale")):
        raise LifecycleError("Invalid control response")
    return result


def status(state: StateDirectory) -> dict:
    try:
        with FileLock(state.path / "instance.lock"):
            return {"protocol": PROTOCOL, "status": "stopped"}
    except InstanceBusy:
        return request(state, "status")


def _write_endpoint(state: StateDirectory, value: dict):
    fd, temporary = tempfile.mkstemp(prefix="control-", suffix=".json", dir=state.path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, state.endpoint_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _acquire_instance_lock(path: Path) -> FileLock:
    # A status probe can hold the lock for an instant before this process starts.
    deadline = time.monotonic() + INSTANCE_LOCK_RETRY
    while True:
        try:
            return FileLock(path)
        except InstanceBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.025)


class Instance:
    """The daemon owns the lock and answers authenticated status/stop requests."""

    def __init__(self, directory: Path, inherited_fd=None, *, identity: dict | None = None):
        if inherited_fd is not None:
            raise LifecycleError("Windows instances do not inherit a lock descriptor")
        self.state = StateDirectory(directory)
        self.lock = None
        self.listener = None
        self.thread = None
        self.stop_event = threading.Event()
        self.closed = threading.Event()
        self.instance_id = str(uuid.uuid4())
        self.token = secrets.token_hex(32)
        self.snapshot_lock = threading.Lock()
        self.details = {**(identity or {}), "startedAt": time.time(), "ready": False}
        self.health_deadline = time.monotonic() + STARTUP_TIMEOUT
        try:
            self.lock = _acquire_instance_lock(self.state.path / "instance.lock")
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(16)
            self.listener.settimeout(0.1)
            _write_endpoint(self.state, {"protocol": PROTOCOL, "instanceId": self.instance_id,
                                         "token": self.token, "port": self.listener.getsockname()[1]})
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
                    action = value.get("action")
                    token = value.get("token")
                    if (type(value.get("protocol")) is not int or value["protocol"] != PROTOCOL
                            or not isinstance(action, str) or action not in ("status", "stop")
                            or not isinstance(token, str) or not hmac.compare_digest(token, self.token)):
                        raise LifecycleError("Unsupported control request")
                    with self.snapshot_lock:
                        details = deepcopy(self.details)
                        if "health" in details:
                            details["health"] = health_view(details["health"], self.health_deadline,
                                                            time.monotonic())
                    result = "stopping" if self.stop_event.is_set() else (
                        "running" if details["ready"] else "starting")
                    if action == "stop":
                        if value.get("instanceId") != self.instance_id:
                            result = "stale"
                        else:
                            self.stop_event.set()
                            result = "stopping"
                    _send(connection, {**details, "protocol": PROTOCOL, "instanceId": self.instance_id,
                                       "pid": os.getpid(), "status": result})
                except (OSError, ValueError, LifecycleError):
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
        if self.lock is not None:
            try:
                endpoint = _read_endpoint(self.state)
                if endpoint["instanceId"] == self.instance_id:
                    self.state.endpoint_path.unlink()
            except (OSError, LifecycleError):
                pass
            self.lock.close()
            self.lock = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _operation_lock(state: StateDirectory, deadline: float) -> FileLock:
    while time.monotonic() < deadline:
        try:
            return FileLock(state.path / "launch.lock")
        except InstanceBusy:
            time.sleep(0.025)
    raise LifecycleError("Another start/stop operation did not complete before the deadline")


def _stop_instance(state: StateDirectory, current: dict, deadline: float) -> dict:
    if current["status"] == "stopped":
        return current
    result = request(state, "stop", current["instanceId"])
    if result["status"] != "stopping":
        raise LifecycleError("Sidebar refused the stop request")
    until = min(deadline, time.monotonic() + CONTROL_TIMEOUT)
    while time.monotonic() < until:
        try:
            latest = status(state)
        except LifecycleError:
            latest = None
        if latest is not None and latest["status"] == "stopped":
            child = _OWNED_CHILDREN.pop(current.get("pid"), None)
            if child is not None:
                try:
                    child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    _OWNED_CHILDREN[child.pid] = child
            return latest
        time.sleep(0.05)
    raise LifecycleError("Sidebar did not stop before the deadline")


def stop(state: StateDirectory) -> dict:
    deadline = time.monotonic() + OPERATION_TIMEOUT
    with _operation_lock(state, deadline):
        return _stop_instance(state, status(state), deadline)


def start(state: StateDirectory, command: list[str], *, expected: dict) -> dict:
    deadline = time.monotonic() + OPERATION_TIMEOUT
    with _operation_lock(state, deadline):
        current = status(state)
        if current["status"] != "stopped":
            if (current["status"] == "running" and current.get("ready") is True
                    and all(current.get(key) == expected[key] for key in ("version", "fingerprint"))):
                return current
            _stop_instance(state, current, deadline)
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
        try:
            until = min(deadline, time.monotonic() + STARTUP_TIMEOUT)
            while time.monotonic() < until:
                if child.poll() is not None:
                    raise LifecycleError("Sidebar initialization failed")
                try:
                    current = status(state)
                except LifecycleError:
                    current = None
                if current is not None and current.get("status") == "running" and current.get("ready") is True:
                    if all(current.get(key) == expected[key] for key in ("version", "fingerprint")):
                        _OWNED_CHILDREN[child.pid] = child
                        return current
                    raise LifecycleError("Source changed during initialization")
                time.sleep(0.05)
            raise LifecycleError("Sidebar initialization timed out")
        except BaseException:
            if child.poll() is None:
                child.terminate()  # Only the child created by this start operation.
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--port", type=int)
    parser.add_argument("--interval", type=float)
    args = parser.parse_args(argv)
    if args.action != "start" and (args.port is not None or args.interval is not None):
        parser.error("--port and --interval are start options")
    if args.action == "start" and args.json:
        parser.error("--json is a status/stop option")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.interval is not None and not 0.1 <= args.interval <= 3600:
        parser.error("--interval must be between 0.1 and 3600")
    state = StateDirectory(state_directory())
    try:
        if args.action == "start":
            runtime = Path(__file__).with_name("codex_token_sidebar.py")
            command = [sys.executable, "-B", str(runtime), "--log-file", str(state.path / "sidebar.log"),
                       "--quiet"]
            if args.port is not None:
                command.extend(("--port", str(args.port)))
            if args.interval is not None:
                command.extend(("--interval", str(args.interval)))
            result = start(state, command, expected=build_identity(runtime))
        else:
            result = status(state) if args.action == "status" else stop(state)
        print(json.dumps(result) if args.json else f"Codex Token Sidebar is {result['status']}"
              + (f" (pid {result['pid']})" if "pid" in result else ""))
        return 0
    except (OSError, ValueError, LifecycleError) as exc:
        print(f"Codex Token Sidebar: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
