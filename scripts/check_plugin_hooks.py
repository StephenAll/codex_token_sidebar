#!/usr/bin/env python3
"""Explicit read-only plugin/Hook discovery via a selected Desktop app-server.

No thread, turn, execution or trust methods are sent. Output omits commands and
unrelated hooks; callers should still treat paths and loading errors as private.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time

MAX_OUTPUT = 2 * 1024 * 1024
READ_METHODS = {"initialize", "plugin/read", "hooks/list"}


class CheckError(RuntimeError):
    pass


class AppServer:
    def __init__(self, executable: Path | list[str], cwd: Path, timeout: float):
        self.deadline = time.monotonic() + timeout
        command = [str(executable)] if isinstance(executable, Path) else list(executable)
        self.process = subprocess.Popen([*command, "app-server", "--listen", "stdio://"],
                                        cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
        self.condition = threading.Condition()
        self.buffer = bytearray()
        self.received = 0
        self.stream_error = None
        self.identifier = 0
        self.reader = threading.Thread(target=self._read_stdout, name="app-server-output", daemon=True)
        self.reader.start()

    def _read_stdout(self):
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                with self.condition:
                    if not chunk:
                        self.stream_error = "app-server closed"
                    else:
                        self.received += len(chunk)
                        if self.received > MAX_OUTPUT:
                            self.stream_error = "app-server output exceeded limit"
                        else:
                            self.buffer.extend(chunk)
                    self.condition.notify_all()
                    if self.stream_error:
                        return
        except OSError:
            with self.condition:
                self.stream_error = "app-server output closed"
                self.condition.notify_all()

    def send(self, value):
        try:
            self.process.stdin.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CheckError("app-server input closed") from exc

    def call(self, method, params):
        if method not in READ_METHODS:
            raise CheckError("non-read-only method rejected")
        self.identifier += 1
        self.send({"id": self.identifier, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise CheckError(f"app-server timed out during {method}")
            with self.condition:
                while b"\n" not in self.buffer:
                    if self.stream_error:
                        raise CheckError(self.stream_error)
                    remaining = self.deadline - time.monotonic()
                    if remaining <= 0:
                        raise CheckError(f"app-server timed out during {method}")
                    self.condition.wait(timeout=remaining)
                line, _, tail = self.buffer.partition(b"\n")
                self.buffer = bytearray(tail)
            try:
                value = json.loads(line)
            except (ValueError, RecursionError) as exc:
                raise CheckError("invalid app-server JSON") from exc
            if not isinstance(value, dict):
                raise CheckError("invalid app-server response")
            if value.get("id") != self.identifier:
                continue
            if "error" in value:
                raise CheckError(f"app-server rejected {method}")
            result = value.get("result")
            if not isinstance(result, dict):
                raise CheckError(f"invalid result for {method}")
            return result

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()  # Only the child this diagnostic owns.
                self.process.wait(timeout=2)
        self.reader.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


def summarize(detail, listing, expected_id):
    plugin = detail.get("plugin")
    if not isinstance(plugin, dict) or not isinstance(plugin.get("summary"), dict):
        raise CheckError("plugin/read returned no plugin summary")
    summary = plugin["summary"]
    if summary.get("id") != expected_id:
        raise CheckError("plugin/read identity mismatch")
    entries = listing.get("data")
    if not isinstance(entries, list):
        raise CheckError("hooks/list returned no entries")
    hooks, errors, warnings = [], [], []
    for entry in entries:
        if not isinstance(entry, dict):
            raise CheckError("invalid hooks/list entry")
        for hook in entry.get("hooks", []):
            if not isinstance(hook, dict) or hook.get("pluginId") != expected_id:
                continue
            hooks.append({key: hook.get(key) for key in (
                "pluginId", "eventName", "enabled", "trustStatus", "currentHash", "key",
                "source", "sourcePath", "handlerType", "matcher", "async", "timeoutSec")})
        for error in entry.get("errors", []):
            if isinstance(error, dict):
                errors.append({key: str(error.get(key, ""))[:512] for key in ("path", "message")})
        warnings.extend(str(value)[:512] for value in entry.get("warnings", []))
    ready = (summary.get("installed") is True and summary.get("enabled") is True and bool(hooks)
             and all(h.get("enabled") is True and h.get("trustStatus") in ("trusted", "managed") for h in hooks)
             and not errors)
    return {"plugin": {key: summary.get(key) for key in (
                "id", "name", "installed", "enabled", "version", "localVersion")},
            "marketplaceName": plugin.get("marketplaceName"),
            "declaredEvents": [h.get("eventName") for h in plugin.get("hooks", []) if isinstance(h, dict)],
            "hooks": hooks, "loadErrors": errors, "warnings": warnings,
            "ready": ready, "executedHooks": False}


def check(executable, cwd, marketplace_path, plugin_name, timeout):
    market = json.loads(marketplace_path.read_text())
    name = market.get("name", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", plugin_name):
        raise CheckError("invalid plugin or marketplace identity")
    server = AppServer(executable, cwd, timeout)
    try:
        server.call("initialize", {"clientInfo": {"name": "sidebar-hook-check", "version": "1"},
                                   "capabilities": {"experimentalApi": True}})
        server.send({"method": "initialized"})
        detail = server.call("plugin/read", {"pluginName": plugin_name, "marketplacePath": str(marketplace_path)})
        listing = server.call("hooks/list", {"cwds": [str(cwd)]})
        return summarize(detail, listing, plugin_name + "@" + name)
    finally:
        server.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-server", required=True, type=Path, help="Desktop's bundled codex executable")
    parser.add_argument("--marketplace-path", required=True, type=Path)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--plugin", default="codex-token-sidebar")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        parser.error("timeout must be greater than 0 and at most 60 seconds")
    try:
        report = check(args.app_server.resolve(), args.cwd.resolve(), args.marketplace_path.resolve(),
                       args.plugin, args.timeout)
    except (CheckError, OSError, ValueError) as exc:
        report = {"ready": False, "executedHooks": False,
                  "error": str(exc) if isinstance(exc, CheckError) else type(exc).__name__}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
