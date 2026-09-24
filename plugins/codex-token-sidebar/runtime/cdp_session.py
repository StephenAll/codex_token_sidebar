"""Loopback CDP discovery and a serial, persistent WebSocket session."""

from __future__ import annotations

import base64
import http.client
import hashlib
import ipaddress
import io
import json
import logging
import os
import secrets
import socket
import struct
import time
from typing import Any
from urllib.parse import urlparse, parse_qs

LOGGER = logging.getLogger("codex-token-sidebar")


class CdpError(RuntimeError):
    pass


class TargetUnavailable(CdpError):
    target_reason = True


DISCOVERY_BUDGET = 3.0
TARGET_CHECK_INTERVAL = 5.0
STRUCTURE_GRACE = 15.0
TARGET_COOLDOWN = 30.0
MAX_DISCOVERY_BYTES = 512 * 1024
AUXILIARY_ROUTES = {"/avatar-overlay", "/detached-window", "/global-dictation", "/hotkey-window",
                    "/chatgpt/quick-chat", "/chatgpt/quick-chat-prewarm"}


def is_main_url(value: str) -> bool:
    """Packaged Desktop entrypoint, verified against its offline bootstrap bundle."""
    try:
        if not isinstance(value, str) or any(ord(c) < 33 for c in value):
            return False
        parsed = urlparse(value)
        if (parsed.scheme != "app" or parsed.netloc != "-" or parsed.path != "/index.html"
                or parsed.params or parsed.fragment):
            return False
        routes = parse_qs(parsed.query, keep_blank_values=True).get("initialRoute", [])
        if len(routes) > 1:
            return False
        if routes:
            route = routes[0]
            if not route.startswith("/") or route.startswith("//") or "\\" in route:
                return False
            path = route.split("?", 1)[0].split("#", 1)[0]
            if "%" in path or any(path == prefix or path.startswith(prefix + "/") for prefix in AUXILIARY_ROUTES):
                return False
        return True
    except ValueError:
        return False


class _DeadlineReader(io.RawIOBase):
    """HTTP headers and bodies share a deadline, including trickled bytes."""
    def __init__(self, sock, deadline, clock=time.monotonic):
        self.sock, self.deadline, self.clock = sock, deadline, clock

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise CdpError("CDP discovery timed out")
        self.sock.settimeout(remaining)
        return self.sock.recv_into(buffer)


class _DiscoverySocket:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def makefile(self, *args, **kwargs):
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline))

    def close(self):
        # HTTPConnection may close an HTTP/1.0 connection before reading its body.
        # The request's finally block owns the socket until that read completes.
        pass


def _cdp_ports(explicit_port: int | None) -> list[int]:
    if explicit_port:
        return [explicit_port]
    raw = os.environ.get("CODEX_TOKEN_SIDEBAR_CDP_PORT", "9222")
    ports: list[int] = []
    for part in raw.split(","):
        try:
            port = int(part.strip())
        except ValueError:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports or [9222]


def _get_json_list(port: int, *, timeout: float = 0.8) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    owned_socket = None
    try:
        conn.connect()
        owned_socket = conn.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CdpError("CDP discovery timed out")
        conn.sock.settimeout(remaining)
        conn.sock = _DiscoverySocket(conn.sock, deadline)
        conn.request("GET", "/json/list")
        response = conn.getresponse()
        if response.status != 200:
            raise CdpError(f"CDP /json/list returned HTTP {response.status}")
        body = response.read(MAX_DISCOVERY_BYTES + 1)
        if len(body) > MAX_DISCOVERY_BYTES:
            raise CdpError("CDP target list is too large")
        value = json.loads(body.decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise CdpError(f"CDP port {port} unavailable: {exc}") from exc
    finally:
        conn.close()
        if owned_socket:
            owned_socket.close()
    return value if isinstance(value, list) else []


def find_page(explicit_port: int | None, *, preferred_url: str | None = None,
              preferred_port: int | None = None, port_offset: int = 0,
              excluded=(), rejected=(), clock=time.monotonic) -> tuple[int, str]:
    errors: list[str] = []
    candidates = []
    deadline = clock() + DISCOVERY_BUDGET
    reached = False
    ports = _cdp_ports(explicit_port)
    offset = port_offset % len(ports)
    ports = ports[offset:] + ports[:offset]
    if preferred_port in ports:
        ports.remove(preferred_port)
        ports.insert(0, preferred_port)
    for port in ports:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            pages = _get_json_list(port, timeout=min(.8, remaining))
            reached = True
        except CdpError as exc:
            errors.append(str(exc))
            continue
        for page in pages:
            if not isinstance(page, dict):
                continue
            identity, url = page.get("id"), page.get("webSocketDebuggerUrl")
            if (page.get("type") == "page" and isinstance(identity, str) and identity
                    and is_main_url(page.get("url")) and isinstance(url, str)
                    and _is_loopback_url(url) and url not in excluded):
                if url == preferred_url:
                    return port, url
                candidates.append((url in rejected, identity, port, url))
    if candidates:
        _, _, port, url = min(candidates)
        return port, url
    if reached:
        raise TargetUnavailable("No eligible Codex main page (or candidates cooling down)")
    raise CdpError("; ".join(errors) or "Codex Desktop CDP page not found")


MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _endpoint(url: str) -> tuple[str, int, str, str]:
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or 80
        if (parsed.scheme != "ws" or not host or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or any(ord(char) < 33 or ord(char) > 126 for char in url)):
            raise ValueError("unsupported URL")
        # Never resolve arbitrary names; localhost is pinned to a literal address.
        address = "127.0.0.1" if host == "localhost" else host
        if not ipaddress.ip_address(address).is_loopback:
            raise ValueError("non-loopback address")
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        authority = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        return address, port, target, authority
    except ValueError as exc:
        raise CdpError("CDP requires a loopback ws URL") from exc


def _is_loopback_url(url: str) -> bool:
    try:
        _endpoint(url)
        return True
    except CdpError:
        return False


class WebSocket:
    """Bounded RFC 6455 text transport with one total deadline per operation."""

    def __init__(self, sock: socket.socket, buffered: bytes = b"", *, clock=time.monotonic):
        self.sock = sock
        self.buffer = bytearray(buffered)
        self.clock = clock

    def _timeout(self, deadline: float) -> None:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise CdpError("CDP operation timed out")
        self.sock.settimeout(remaining)

    def _read(self, size: int, deadline: float) -> bytes:
        self._timeout(deadline)
        while len(self.buffer) < size:
            self._timeout(deadline)
            chunk = self.sock.recv(min(65536, max(4096, size - len(self.buffer))))
            if not chunk:
                raise CdpError("CDP WebSocket closed")
            self.buffer.extend(chunk)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def send(self, payload: bytes, deadline: float, opcode: int = 1) -> None:
        if len(payload) > MAX_MESSAGE_BYTES:
            raise CdpError("CDP message is too large")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0xFE)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0xFF)
            header.extend(struct.pack("!Q", length))
        mask = secrets.token_bytes(4)
        header.extend(mask)
        header.extend(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._timeout(deadline)
        self.sock.sendall(header)

    def receive(self, deadline: float) -> str:
        message = bytearray()
        fragmented = False
        while True:
            first, second = self._read(2, deadline)
            final, opcode = bool(first & 0x80), first & 15
            if first & 0x70 or second & 0x80:
                raise CdpError("Unsupported WebSocket flags or masked server frame")
            length = second & 127
            encoded_length = length
            if length == 126:
                length = struct.unpack("!H", self._read(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8, deadline))[0]
            if ((encoded_length == 126 and length < 126)
                    or (encoded_length == 127 and length < 65536)):
                raise CdpError("Non-minimal WebSocket length")
            if length > MAX_MESSAGE_BYTES:
                raise CdpError("CDP frame is too large")
            if opcode >= 8 and (not final or length > 125):
                raise CdpError("Invalid WebSocket control frame")
            payload = self._read(length, deadline)
            if opcode == 8:
                if len(payload) == 1:
                    raise CdpError("Invalid WebSocket close frame")
                if payload:
                    code = struct.unpack("!H", payload[:2])[0]
                    if not (code in {1000, 1001, 1002, 1003, 1007, 1008, 1009,
                                     1010, 1011, 1012, 1013, 1014}
                            or 3000 <= code <= 4999):
                        raise CdpError("Invalid WebSocket close code")
                    payload[2:].decode("utf-8")
                self.send(payload, deadline, opcode=8)
                raise CdpError("CDP WebSocket closed by peer")
            if opcode == 9:
                self.send(payload, deadline, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                if fragmented:
                    raise CdpError("New message during fragmented message")
            elif opcode == 0:
                if not fragmented:
                    raise CdpError("Unexpected WebSocket continuation")
            else:
                raise CdpError("Unsupported WebSocket opcode")
            if len(message) + length > MAX_MESSAGE_BYTES:
                raise CdpError("CDP message is too large")
            message.extend(payload)
            if final:
                return message.decode("utf-8")
            fragmented = True

    def close(self) -> None:
        self.sock.close()


def _open_websocket(websocket_url: str) -> WebSocket:
    address, port, target, authority = _endpoint(websocket_url)
    deadline = time.monotonic() + 4
    sock = socket.create_connection((address, port), timeout=4)
    try:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {target} HTTP/1.1\r\nHost: {authority}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        connection = WebSocket(sock)
        connection._timeout(deadline)
        sock.sendall(request.encode("ascii"))
        response = bytearray()
        while b"\r\n\r\n" not in response:
            connection._timeout(deadline)
            chunk = sock.recv(4096)
            if not chunk:
                raise CdpError("CDP WebSocket handshake closed")
            response.extend(chunk)
            if len(response) > MAX_HEADER_BYTES + 4096:
                raise CdpError("CDP WebSocket handshake is too large")
        header, remainder = bytes(response).split(b"\r\n\r\n", 1)
        if len(header) > MAX_HEADER_BYTES:
            raise CdpError("CDP WebSocket handshake is too large")
        lines = header.decode("latin1").split("\r\n")
        if lines[0].split(" ")[:2] != ["HTTP/1.1", "101"]:
            raise CdpError("CDP WebSocket handshake rejected")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if not separator or name.lower() in headers:
                raise CdpError("Malformed or duplicate handshake header")
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode()
        if (headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in {part.strip().lower() for part in headers.get("connection", "").split(",")}
                or headers.get("sec-websocket-accept") != expected
                or "sec-websocket-extensions" in headers
                or "sec-websocket-protocol" in headers):
            raise CdpError("Invalid WebSocket handshake")
        connection.buffer.extend(remainder)
        return connection
    except BaseException:
        sock.close()
        raise


class CdpSession:
    """One caller, one outstanding command; failures close without implicit replay."""

    def __init__(self, explicit_port: int | None = None, *, timeout: float = 5.0,
                 clock=time.monotonic):
        self.explicit_port = explicit_port
        self.timeout = timeout
        self.clock = clock
        self._connection: WebSocket | None = None
        self._next_id = 0
        self._target_url = None
        self._target_port = None
        self._scan_offset = 0
        self._last_target_check = float("-inf")
        self._unrecognized_since = None
        self._cooldown = {}
        self._rejected = {}
        self._target_failures = 0

    def observe_page(self, page_url: str, recognized: bool) -> None:
        if not is_main_url(page_url):
            self._reject_target("Selected target left the Codex main page")
        if recognized:
            self._unrecognized_since = None
            self._rejected.pop(self._target_url, None)
        elif self._unrecognized_since is None:
            self._unrecognized_since = self.clock()
        elif self.clock() - self._unrecognized_since >= STRUCTURE_GRACE:
            self._reject_target("Selected target lacks the Codex page structure")

    def _reject_target(self, reason):
        if self._target_url:
            self._cooldown[self._target_url] = self.clock() + TARGET_COOLDOWN
            self._rejected[self._target_url] = True
            if len(self._rejected) > 128:
                self._rejected.pop(next(iter(self._rejected)))
        self.close()
        self._target_url = None
        self._target_port = None
        self._target_failures = 0
        raise TargetUnavailable(reason)

    def close(self) -> None:
        connection, self._connection = self._connection, None
        self._unrecognized_since = None
        if connection:
            try:
                connection.close()
            except OSError:
                pass

    def evaluate(self, expression: str) -> Any:
        try:
            now = self.clock()
            if self._connection is None or now - self._last_target_check >= TARGET_CHECK_INTERVAL:
                self._cooldown = {url: until for url, until in self._cooldown.items() if until > now}
                offset = self._scan_offset
                self._scan_offset += 1
                port, url = find_page(self.explicit_port, preferred_url=self._target_url,
                                     preferred_port=self._target_port, port_offset=offset,
                                     excluded=self._cooldown, rejected=self._rejected, clock=self.clock)
                self._last_target_check = self.clock()
                if url != self._target_url:
                    self.close()
                    self._unrecognized_since = None
                    self._target_failures = 0
                self._target_url = url
                self._target_port = port
            if self._connection is None:
                url = self._target_url
                self._connection = _open_websocket(url)
            self._next_id += 1
            message_id = self._next_id
            request = {"id": message_id, "method": "Runtime.evaluate", "params": {
                "expression": expression, "returnByValue": True, "awaitPromise": True}}
            deadline = self.clock() + self.timeout
            self._connection.send(json.dumps(request, separators=(",", ":")).encode(), deadline)
            while True:
                response = json.loads(self._connection.receive(deadline))
                if not isinstance(response, dict):
                    raise CdpError("Invalid CDP response")
                if "method" in response:
                    if response["method"] in {"Inspector.detached", "Runtime.executionContextsCleared"}:
                        raise CdpError("CDP page execution context changed")
                    continue
                if response.get("id") != message_id:
                    continue
                if "error" in response:
                    raise CdpError("CDP Runtime.evaluate returned an error")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise CdpError("Missing CDP evaluation result")
                remote = result.get("result")
                if not isinstance(remote, dict) or "exceptionDetails" in result or remote.get("subtype") == "error":
                    raise CdpError("CDP Runtime.evaluate exception")
                self._target_failures = 0
                return remote.get("value")
        except (CdpError, OSError, ValueError) as exc:
            self.close()
            if self._target_url and not isinstance(exc, TargetUnavailable):
                self._target_failures += 1
                if self._target_failures >= 3:
                    self._reject_target("Selected CDP target failed three consecutive operations")
            if isinstance(exc, CdpError):
                raise
            raise CdpError(f"CDP transport failure: {type(exc).__name__}") from exc
