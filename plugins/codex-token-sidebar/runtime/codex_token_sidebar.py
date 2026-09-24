#!/usr/bin/env python3
"""Local Codex usage reader and CDP sidebar injector.

The runtime deliberately has no third-party dependencies. It reads the local
Codex rollout files, discovers the active Codex Desktop page through the
loopback CDP endpoint, and evaluates the bundled DOM injector in that page.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time
from typing import Any

from cdp_session import CdpError, CdpSession
from usage import aggregate_events, parse_usage_text
from usage_reader import UsageReader
from rate_sync import RateSync
from rollout_discovery import RolloutCatalog
from state_index import NativeStateIndex
if os.name == "nt":
    from windows_lifecycle import Instance, InstanceBusy, LifecycleError, state_directory
else:
    from lifecycle import Instance, InstanceBusy, LifecycleError, state_directory
from identity import build_identity
from health import RuntimeHealth, PAGE_TIMEOUT_MS

LOGGER = logging.getLogger("codex-token-sidebar")
SCHEMA_VERSION = 3
STATE_EXPRESSION = ("window.__codexTokenSidebarInstalled"
                    " && typeof window.__codexTokenSidebarState === 'function'"
                    " && typeof window.__codexTokenSidebarUpdate === 'function'"
                    " ? window.__codexTokenSidebarState() : null;")


class SidebarChanged(RuntimeError):
    """The selection changed during a cycle; retry normally without reconnecting."""


class SidebarWaiting(RuntimeError):
    """A URL-qualified page has not yet exposed its Desktop shell."""


def default_log_path() -> Path:
    return state_directory() / "sidebar.log"


def configure_logging(
    log_file: Path | None = None,
    level_name: str = "INFO",
    console: bool = True,
) -> Path:
    path = log_file or default_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, level_name.upper(), logging.INFO)
    LOGGER.setLevel(level)
    LOGGER.handlers.clear()
    LOGGER.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    try:
        file_handler = RotatingFileHandler(
            path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)
    except OSError as exc:
        # Keep the runtime usable when the log directory is unavailable.
        logging.basicConfig(level=level, format="%(levelname)s %(message)s")
        LOGGER.warning("file logging unavailable path=%s error=%s", path, exc)
    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        LOGGER.addHandler(stream_handler)
    LOGGER.info(
        "runtime started log_file=%s level=%s pid=%s",
        path,
        logging.getLevelName(level),
        os.getpid(),
    )
    return path


@lru_cache(maxsize=1)
def _injector_bundle() -> tuple[str, str]:
    source = (Path(__file__).with_name("injector.js")).read_text(encoding="utf-8")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return source.replace("__CODEX_TOKEN_SIDEBAR_HASH__", json.dumps(digest)), digest


def push_once(reader: UsageReader, explicit_port: int | None, *,
              session: CdpSession | None = None, health: RuntimeHealth | None = None) -> dict[str, Any]:
    owned = session is None
    session = session or CdpSession(explicit_port)
    try:
        return _push_snapshot(reader, session, health)
    finally:
        if owned:
            session.close()


def _valid_state(state: Any, digest: str) -> bool:
    return (isinstance(state, dict) and type(state.get("schemaVersion")) is int
            and state["schemaVersion"] == SCHEMA_VERSION
            and state.get("scriptHash") == digest and isinstance(state.get("pageEpoch"), str)
            and bool(state["pageEpoch"])
            and type(state.get("selectionEpoch")) is int
            and state["selectionEpoch"] >= 0
            and type(state.get("probeId")) is int
            and state["probeId"] > 0
            and type(state.get("mounted")) is bool
            and type(state.get("targetRecognized")) is bool
            and isinstance(state.get("pageUrl"), str)
            and (state.get("conversationId") is None or isinstance(state.get("conversationId"), str)))


def _push_snapshot(reader: UsageReader, session: CdpSession, health: RuntimeHealth | None = None) -> dict[str, Any]:
    source, digest = _injector_bundle()
    previous_read = reader.read_status
    timeout = health.timeout_ms if health else PAGE_TIMEOUT_MS

    def probe_expression():
        signal = json.dumps({"reader": reader.read_status, "timeoutMs": timeout}, separators=(",", ":"))
        return STATE_EXPRESSION.replace("SidebarState()", f"SidebarState({signal})")

    def evaluate(expression):
        if health:
            health.stage = "cdp"
        result = session.evaluate(expression)
        if health:
            health.data["cdp"] = "connected"
            health.stage = "protocol"
        return result

    page_state = evaluate(probe_expression())
    if not _valid_state(page_state, digest):
        page_state = evaluate(source + "\n;" + probe_expression())
    if not _valid_state(page_state, digest):
        raise CdpError("Sidebar installation/state verification failed")
    conversation_id = page_state.get("conversationId")
    if not isinstance(conversation_id, str):
        conversation_id = None
    if health:
        health.page(page_state)
        health.stage = "target"
    session.observe_page(page_state["pageUrl"], page_state["targetRecognized"])
    if not page_state["targetRecognized"]:
        if health:
            health.data["state"] = "waiting_target"
        raise SidebarWaiting("Waiting for the Codex page structure")
    if health:
        health.stage = "reader"
    payload = reader.snapshot(conversation_id)
    # Ask the page what it holds: a Python last-sent cache would suppress
    # recovery after a reload or a lost update with unchanged usage.
    if (page_state.get("revision") != payload["revision"]
            or page_state.get("dataConversationId") != conversation_id
            or page_state.get("dataSelectionEpoch") != page_state["selectionEpoch"]):
        envelope = {**payload, **{key: page_state[key] for key in (
            "schemaVersion", "pageEpoch", "selectionEpoch", "probeId", "pageUrl")},
                    "health": {"reader": reader.read_status, "timeoutMs": timeout}}
        payload_json = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        acknowledgment = evaluate(
            "window.__codexTokenSidebarUpdate ? "
            f"window.__codexTokenSidebarUpdate({payload_json}) : "
            '{status:"needs_install"};')
        if isinstance(acknowledgment, dict) and acknowledgment.get("status") in {"stale", "needs_install"}:
            raise SidebarChanged("Sidebar state changed before update")
        if (not isinstance(acknowledgment, dict) or acknowledgment.get("status") != "accepted"
                or type(acknowledgment.get("mounted")) is not bool
                or any(acknowledgment.get(key) != envelope[key] for key in (
                    "schemaVersion", "pageEpoch", "selectionEpoch", "probeId", "conversationId", "revision", "pageUrl"))):
            raise CdpError("Sidebar update acknowledgment mismatch")
        page_state = {**page_state, "mounted": acknowledgment["mounted"]}
        LOGGER.debug("sidebar update accepted conversation_id=%s", conversation_id)
    elif previous_read != reader.read_status:
        # A diagnostic transition must not resend or revise unchanged statistics.
        checked = evaluate(probe_expression())
        if not _valid_state(checked, digest) or any(checked.get(key) != page_state.get(key) for key in (
                "pageEpoch", "selectionEpoch", "conversationId", "revision", "dataSelectionEpoch", "dataConversationId", "pageUrl")):
            raise SidebarChanged("Sidebar changed during health update")
        page_state = checked
    if health:
        health.page(page_state)
        health.synchronized(payload, reader.read_status)
    return payload


def _retry_delay(interval: float, failures: int) -> float:
    return min(30.0, max(0.5, interval) * (2 ** min(max(0, failures - 1), 10)))


def discovery_mode() -> str:
    """Read the restart-scoped mode; invalid explicit values are startup errors."""
    mode = os.environ.get("CODEX_TOKEN_SIDEBAR_DISCOVERY", "filesystem")
    if mode not in {"auto", "filesystem"}:
        raise ValueError("CODEX_TOKEN_SIDEBAR_DISCOVERY must be auto or filesystem")
    return mode


def create_reader() -> UsageReader:
    """Derive both storage paths from one home, without opening either at startup."""
    mode = discovery_mode()
    configured = os.environ.get("CODEX_HOME")
    home = Path(configured).expanduser().absolute() if configured else Path.home() / ".codex"
    sessions = home / "sessions"
    index = NativeStateIndex(home / "state_5.sqlite", sessions) if mode == "auto" else None
    return UsageReader(sessions, discoverer=RolloutCatalog(sessions, index=index))


def run_daemon(interval: float, explicit_port: int | None, once: bool, *, stop_event=None, on_ready=None, on_health=None) -> int:
    reader = create_reader()
    if not once and os.environ.get("CODEX_TOKEN_SIDEBAR_RATE_SYNC", "1") != "0":
        reader.rate_source = RateSync(state_directory() / "credits-rates.json")
        reader.rate_source.poll()
    session = CdpSession(explicit_port)
    health = RuntimeHealth(interval=interval)
    last_error = ""
    last_state = None
    failures = 0

    def publish():
        if on_health:
            on_health(health.snapshot(), health.deadline)

    try:
        publish()
        if on_ready is not None:
            on_ready()
        while stop_event is None or not stop_event.is_set():
            delay = max(0.5, interval)
            health.begin()
            health.stage = "cdp"
            publish()
            try:
                payload = push_once(reader, explicit_port, session=session, health=health)
                if last_error:
                    LOGGER.info("runtime cycle restored")
                    last_error = ""
                failures = 0
                if once:
                    print(json.dumps({**payload, "health": health.snapshot()}, ensure_ascii=False), flush=True)
                    return 0
            except SidebarWaiting:
                failures = 0
                health.data.update(state="waiting_target", error=None)
                if once:
                    return 2
            except SidebarChanged:
                failures = 0
                health.data.update(state="selection_changed", error=None)
                if once:
                    return 2
            except Exception as exc:
                session.close()
                if getattr(exc, "target_reason", False):
                    health.stage = "target"
                # CDP errors are generated locally without page contents. Other
                # exception messages may include file or payload data.
                reason = " ".join(str(exc).split())[:200] if isinstance(exc, CdpError) else type(exc).__name__
                health.fail(health.stage, exc, reason)
                failures += 1
                delay = _retry_delay(interval, failures)
                message = health.stage + ":" + reason
                if message != last_error:
                    LOGGER.warning("runtime cycle failed error=%s", message)
                    last_error = message
                if once:
                    return 2
            finally:
                health.finish(delay)
                publish()
                state = (health.data["state"], health.data["reader"])
                if state != last_state:
                    LOGGER.info("runtime health state=%s reader=%s", *state)
                    last_state = state
            if stop_event is None:
                time.sleep(delay)
            else:
                stop_event.wait(delay)
        return 0
    finally:
        session.close()
        reader.close()


def run_self_test() -> int:
    fixture = "\n".join(
        [
            json.dumps(
                {
                    "type": "turn_context",
                    "payload": {"model": "gpt-6-astra"},
                }
            ),
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": "2026-09-20T00:00:00Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 40,
                                "output_tokens": 20,
                                "reasoning_output_tokens": 5,
                                "total_tokens": 125,
                            },
                            "last_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 40,
                                "output_tokens": 20,
                                "reasoning_output_tokens": 5,
                                "total_tokens": 125,
                            },
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": "2026-09-20T00:01:00Z",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 150,
                                "cached_input_tokens": 60,
                                "output_tokens": 30,
                                "reasoning_output_tokens": 8,
                                "total_tokens": 188,
                            }
                        },
                    },
                }
            ),
        ]
    )
    events = parse_usage_text(fixture, "00000000-0000-0000-0000-000000000000")
    report = aggregate_events(events)
    assert len(events) == 2
    assert report["total"]["total_tokens"] == 188
    assert report["total"]["cached_input_tokens"] == 60
    assert report["by_model"][0]["model"] == "gpt-6-astra"
    print("self_test=passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=os.environ.get("CODEX_TOKEN_SIDEBAR_LOG_LEVEL", "INFO").upper(),
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--lifecycle-lock-fd", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_test:
        return run_self_test()
    try:
        discovery_mode()
    except ValueError as exc:
        parser.error(str(exc))
    if args.once:
        configure_logging(args.log_file, args.log_level, console=not args.quiet)
        return run_daemon(args.interval, args.port, True)
    try:
        identity = build_identity(Path(__file__))
        with Instance(state_directory(), args.lifecycle_lock_fd, identity=identity) as instance:
            configure_logging(args.log_file, args.log_level, console=not args.quiet)
            return run_daemon(args.interval, args.port, False, stop_event=instance.stop_event,
                              on_ready=instance.mark_ready, on_health=instance.publish_health)
    except InstanceBusy:
        print("Codex Token Sidebar already has an active instance")
        return 0
    except (OSError, LifecycleError) as exc:
        print(f"Codex Token Sidebar initialization failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
