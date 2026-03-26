from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import urlsplit

from journey.tools._webhook_shared import build_request_payload, normalize_path

_CONTROL_CREATE_PATH = "/v1/webhook-endpoints"
_CONTROL_NEXT_SUFFIX = "/requests/next"
_PUBLIC_WEBHOOK_PREFIX = "/webhooks/"


@dataclass(frozen=True)
class RunningCloudStub:
    api_key: str
    base_url: str
    public_base_url: str


@dataclass(frozen=True)
class _StubEndpoint:
    endpoint_id: str
    path: str


@dataclass
class _StoredEndpoint:
    endpoint: _StubEndpoint
    requests: list[dict[str, Any]] = field(default_factory=list)


class _StubStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._endpoints: dict[str, _StoredEndpoint] = {}

    def create_endpoint(self, *, path: str) -> _StubEndpoint:
        with self._lock:
            self._counter += 1
            endpoint = _StubEndpoint(endpoint_id=f"stub-{self._counter}", path=path)
            self._endpoints[endpoint.endpoint_id] = _StoredEndpoint(endpoint=endpoint)
            return endpoint

    def get_endpoint(self, endpoint_id: str) -> _StubEndpoint | None:
        with self._lock:
            stored = self._endpoints.get(endpoint_id)
            return None if stored is None else stored.endpoint

    def enqueue_request(self, *, endpoint_id: str, payload: dict[str, Any]) -> bool:
        with self._lock:
            stored = self._endpoints.get(endpoint_id)
            if stored is None:
                return False
            stored.requests.append(payload)
            return True

    def dequeue_request(self, *, endpoint_id: str) -> dict[str, Any] | None:
        with self._lock:
            stored = self._endpoints.get(endpoint_id)
            if stored is None or not stored.requests:
                return None
            return stored.requests.pop(0)


class _CloudStub:
    def __init__(self, *, host: str, port: int, api_key: str, public_base_url: str | None) -> None:
        self.host = host
        self.port = port
        self.api_key = api_key
        self.public_base_url = public_base_url
        self.store = _StubStore()
        self._server = _CloudStubHTTPServer((host, port), self)
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def visible_public_base_url(self) -> str:
        return self.public_base_url or self.base_url

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        self._thread = thread
        return thread

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None


class _CloudStubHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], app: _CloudStub) -> None:
        self.app = app
        super().__init__(server_address, _CloudStubHandler)


class _CloudStubHandler(BaseHTTPRequestHandler):
    server: _CloudStubHTTPServer

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
        if parsed.path == "/_health":
            self._send_json({"status": "ok", "service": "public-cloud-stub"})
            return
        if parsed.path == _CONTROL_CREATE_PATH:
            self._handle_create_endpoint()
            return

        endpoint_id = self._match_next_request_route(parsed.path)
        if endpoint_id is not None:
            self._handle_next_request(endpoint_id=endpoint_id)
            return

        if parsed.path.startswith(_PUBLIC_WEBHOOK_PREFIX):
            self._handle_public_ingest(parsed=parsed)
            return

        self._send_json_error(HTTPStatus.NOT_FOUND, "Unknown cloud stub route.")

    def _handle_create_endpoint(self) -> None:
        if self.command != "POST":
            self._send_json_error(HTTPStatus.METHOD_NOT_ALLOWED, "Use POST to create endpoints.")
            return
        if not self._is_authorized():
            return

        payload = self._read_json_body()
        if payload is None:
            return

        try:
            path = normalize_path(payload.get("path"), owner="cloud stub API")
        except (TypeError, ValueError) as exc:
            self._send_json_error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        endpoint = self.server.app.store.create_endpoint(path=path)
        self._send_json(
            {
                "endpoint_id": endpoint.endpoint_id,
                "path": endpoint.path,
                "url": f"{self.server.app.visible_public_base_url}{_PUBLIC_WEBHOOK_PREFIX}{endpoint.endpoint_id}{endpoint.path}",
            },
            status=HTTPStatus.CREATED,
        )

    def _handle_next_request(self, *, endpoint_id: str) -> None:
        if self.command != "POST":
            self._send_json_error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "Use POST to fetch queued webhook requests.",
            )
            return
        if not self._is_authorized():
            return

        self._read_request_body()
        endpoint = self.server.app.store.get_endpoint(endpoint_id)
        if endpoint is None:
            self._send_json_error(HTTPStatus.NOT_FOUND, "Unknown webhook endpoint.")
            return

        payload = self.server.app.store.dequeue_request(endpoint_id=endpoint_id)
        if payload is None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self._send_json(payload)

    def _handle_public_ingest(self, *, parsed: Any) -> None:
        endpoint_id, path = self._split_public_path(parsed.path)
        if endpoint_id is None or path is None:
            self._send_json_error(HTTPStatus.NOT_FOUND, "Unknown webhook endpoint.")
            return

        endpoint = self.server.app.store.get_endpoint(endpoint_id)
        if endpoint is None or endpoint.path != path:
            self._send_json_error(HTTPStatus.NOT_FOUND, "Unknown webhook endpoint.")
            return

        raw_body = self._read_request_body()
        payload = build_request_payload(
            method=self.command,
            url=f"{self.server.app.visible_public_base_url}{parsed.path}"
            + (f"?{parsed.query}" if parsed.query else ""),
            path=path,
            query_string=parsed.query,
            headers=self.headers,
            raw_body=raw_body,
        )
        self.server.app.store.enqueue_request(endpoint_id=endpoint_id, payload=payload)
        self._send_json({"queued": True}, status=HTTPStatus.ACCEPTED)

    def _is_authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            self._send_json_error(HTTPStatus.UNAUTHORIZED, "Missing bearer token.")
            return False
        api_key = header[len(prefix) :].strip()
        if api_key != self.server.app.api_key:
            self._send_json_error(HTTPStatus.UNAUTHORIZED, "Invalid bearer token.")
            return False
        return True

    def _read_json_body(self) -> dict[str, object] | None:
        raw_body = self._read_request_body()
        if not raw_body:
            return {}
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json_error(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON.")
            return None
        if not isinstance(payload, dict):
            self._send_json_error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            return None
        return payload

    def _read_request_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _send_json_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    @staticmethod
    def _match_next_request_route(path: str) -> str | None:
        prefix = f"{_CONTROL_CREATE_PATH}/"
        if not path.startswith(prefix) or not path.endswith(_CONTROL_NEXT_SUFFIX):
            return None
        endpoint_id = path[len(prefix) : -len(_CONTROL_NEXT_SUFFIX)]
        return endpoint_id or None

    @staticmethod
    def _split_public_path(path: str) -> tuple[str | None, str | None]:
        suffix = path[len(_PUBLIC_WEBHOOK_PREFIX) :]
        if "/" not in suffix:
            return None, None
        endpoint_id, remainder = suffix.split("/", 1)
        if not endpoint_id:
            return None, None
        return endpoint_id, "/" + remainder


@contextmanager
def serve_in_background(
    *,
    api_key: str = "journey-test-key",
    public_base_url: str | None = None,
) -> Iterator[RunningCloudStub]:
    port = _free_port()
    stub = _CloudStub(
        host="127.0.0.1",
        port=port,
        api_key=api_key,
        public_base_url=public_base_url,
    )
    stub.start()
    try:
        _wait_until_ready(f"{stub.base_url}/_health")
        yield RunningCloudStub(
            api_key=api_key,
            base_url=stub.base_url,
            public_base_url=stub.visible_public_base_url,
        )
    finally:
        stub.shutdown()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(url: str) -> None:
    deadline = time.monotonic() + 5
    while True:
        try:
            with urllib.request.urlopen(url, timeout=0.2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for the public cloud stub at {url}.")
        time.sleep(0.01)
