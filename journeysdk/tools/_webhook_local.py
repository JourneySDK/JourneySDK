"""Local webhook host runtime for the official webhook tool."""

from __future__ import annotations

import socket
import threading
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from ._webhook_shared import build_request_payload, normalize_path as normalize_path_base

CONTROL_PREFIX = "/_journey/webhooks/"
_LOOPBACK_HOST = "127.0.0.1"
_PUBLIC_HOST = "localhost"


def build_public_url(*, port: int, path: str) -> str:
    return f"http://{_PUBLIC_HOST}:{port}{path}"


def build_poll_url(*, port: int, path: str) -> str:
    encoded_path = quote(path, safe="")
    return f"http://{_LOOPBACK_HOST}:{port}{CONTROL_PREFIX}poll?path={encoded_path}"


def normalize_path(path: str) -> str:
    normalized = normalize_path_base(path, owner="host_webhook_endpoint")
    if normalized == "/":
        raise ValueError("host_webhook_endpoint(..., path=...) expects a non-root path.")
    if normalized.startswith(CONTROL_PREFIX):
        raise ValueError(
            "Webhook paths under '/_journey/webhooks/' are reserved for the local webhook host."
        )
    return normalized


def is_port_open(port: int) -> bool:
    try:
        with socket.create_connection((_LOOPBACK_HOST, port), timeout=0.05):
            return True
    except OSError:
        return False


@dataclass
class _WebhookQueue:
    requests: deque[dict[str, Any]] = field(default_factory=deque)


@dataclass
class _LocalWebhookHost:
    port: int
    server: ThreadingHTTPServer
    thread: threading.Thread
    lock: threading.Lock = field(default_factory=threading.Lock)
    queues: dict[str, _WebhookQueue] = field(default_factory=dict)

    def ensure_path(self, path: str) -> None:
        with self.lock:
            self.queues.setdefault(path, _WebhookQueue())

    def enqueue(self, path: str, payload: dict[str, Any]) -> None:
        with self.lock:
            queue = self.queues.setdefault(path, _WebhookQueue())
            queue.requests.append(payload)

    def dequeue(self, path: str) -> dict[str, Any] | None:
        with self.lock:
            queue = self.queues.get(path)
            if queue is None or not queue.requests:
                return None
            return queue.requests.popleft()


_HOSTS_BY_PORT: dict[int, _LocalWebhookHost] = {}
_HOSTS_LOCK = threading.Lock()


class _WebhookRequestHandler(BaseHTTPRequestHandler):
    server: "_WebhookHTTPServer"

    def do_GET(self) -> None:  # noqa: N802
        self._handle_request()

    def do_POST(self) -> None:  # noqa: N802
        self._handle_request()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle_request()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle_request()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle_request()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_request()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        del format, args

    def _handle_request(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == f"{CONTROL_PREFIX}poll":
            self._handle_poll(parsed.query)
            return
        self._handle_ingest(parsed)

    def _handle_poll(self, raw_query: str) -> None:
        query = parse_qs(raw_query, keep_blank_values=True)
        requested_path = query.get("path", [None])[0]
        if requested_path is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing poll path.")
            return

        payload = self.server.host.dequeue(requested_path)
        if payload is None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return

        encoded = self._encode_json(payload)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _handle_ingest(self, parsed: Any) -> None:
        path = parsed.path
        if path.startswith(CONTROL_PREFIX):
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown control route.")
            return

        if path not in self.server.host.queues:
            self.send_error(HTTPStatus.NOT_FOUND, "Webhook path is not registered.")
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length > 0 else b""
        payload = build_request_payload(
            method=self.command,
            url=build_public_url(port=self.server.host.port, path=path)
            + (f"?{parsed.query}" if parsed.query else ""),
            path=path,
            query_string=parsed.query,
            headers=self.headers,
            raw_body=raw_body,
        )
        self.server.host.enqueue(path, payload)

        response = self._encode_json({"queued": True})
        self.send_response(HTTPStatus.ACCEPTED)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response)

    @staticmethod
    def _encode_json(payload: dict[str, Any]) -> bytes:
        import json

        return json.dumps(payload).encode("utf-8")


class _WebhookHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], host: _LocalWebhookHost) -> None:
        self.host = host
        super().__init__(server_address, _WebhookRequestHandler)


def ensure_local_host(*, port: int, path: str) -> None:
    with _HOSTS_LOCK:
        existing = _HOSTS_BY_PORT.get(port)
        if existing is not None:
            existing.ensure_path(path)
            return

        placeholder = _LocalWebhookHost(
            port=port,
            server=None,  # type: ignore[arg-type]
            thread=None,  # type: ignore[arg-type]
        )
        server = _WebhookHTTPServer((_LOOPBACK_HOST, port), placeholder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        placeholder.server = server
        placeholder.thread = thread
        placeholder.ensure_path(path)
        _HOSTS_BY_PORT[port] = placeholder
        thread.start()


def poll_request(*, port: int, path: str) -> dict[str, Any] | None:
    host = _HOSTS_BY_PORT.get(port)
    if host is None:
        return None
    return host.dequeue(path)


def reset_local_hosts() -> None:
    with _HOSTS_LOCK:
        hosts = list(_HOSTS_BY_PORT.values())
        _HOSTS_BY_PORT.clear()

    for host in hosts:
        host.server.shutdown()
        host.server.server_close()
        host.thread.join(timeout=1)
