from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import urlsplit

from journeysdk.touchpoints._webhook_shared import build_request_payload, normalize_path

_CONTROL_CREATE_PATH = "/v1/webhook-endpoints"
_CONTROL_NEXT_SUFFIX = "/requests/next"
_PUBLIC_WEBHOOK_PREFIX = "/webhooks/"
_EMAIL_INBOX_DEFAULT_PATH = "/v1/email-inboxes/default"
_EMAIL_SEND_PATH = "/v1/email-inboxes/default/messages/send"
_EMAIL_NEXT_PATH = "/v1/email-inboxes/default/messages/next"


@dataclass(frozen=True)
class RunningCloudStub:
    api_key: str
    base_url: str
    public_base_url: str
    default_email_address: str


@dataclass(frozen=True)
class _StubEndpoint:
    endpoint_id: str
    path: str


@dataclass
class _StoredEndpoint:
    endpoint: _StubEndpoint
    requests: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _StubInbox:
    address: str
    mailbox: str


@dataclass
class _StoredEmailMessage:
    payload: dict[str, Any]
    unread: bool = True
    consumed: bool = False


class _StubStore:
    def __init__(self, *, default_inbox: _StubInbox) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._endpoints: dict[str, _StoredEndpoint] = {}
        self._default_inbox = default_inbox
        self._messages: list[_StoredEmailMessage] = []

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

    def default_inbox(self) -> _StubInbox:
        return self._default_inbox

    def send_email(
        self,
        *,
        to: list[str] | None,
        subject: str,
        text_body: str | None,
        html_body: str | None,
        from_address: str | None,
        message_id: str,
    ) -> dict[str, Any]:
        with self._lock:
            sender = from_address or self._default_inbox.address
            recipients = to or [self._default_inbox.address]
            payload = {
                "message_id": message_id,
                "subject": subject,
                "from_address": sender,
                "to": recipients,
                "cc": [],
                "reply_to": None,
                "text_body": text_body,
                "html_body": html_body,
                "headers": {
                    "message-id": message_id,
                    "subject": subject,
                    "from": sender,
                    "to": ", ".join(recipients),
                },
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
            self._messages.append(_StoredEmailMessage(payload=payload))
            return {
                "message_id": message_id,
                "from_address": sender,
                "to": recipients,
                "subject": subject,
                "transport": "cloud",
            }

    def next_email(
        self,
        *,
        subject_contains: str | None,
        from_address: str | None,
        to_address: str | None,
        unread_only: bool,
    ) -> dict[str, Any] | None:
        with self._lock:
            for stored in self._messages:
                if stored.consumed:
                    continue
                if unread_only and not stored.unread:
                    continue
                payload = stored.payload
                if subject_contains is not None and subject_contains not in str(
                    payload.get("subject", "")
                ):
                    continue
                if from_address is not None and payload.get("from_address") != from_address:
                    continue
                if to_address is not None and to_address not in list(payload.get("to", [])):
                    continue
                stored.unread = False
                stored.consumed = True
                return payload
            return None


class _CloudStub:
    def __init__(self, *, host: str, port: int, api_key: str, public_base_url: str | None) -> None:
        self.host = host
        self.port = port
        self.api_key = api_key
        self.public_base_url = public_base_url
        self.default_inbox = _StubInbox(
            address=_default_email_address(api_key),
            mailbox="INBOX",
        )
        self.store = _StubStore(default_inbox=self.default_inbox)
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
        pass

    def _handle_request(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/_health":
            self._send_json({"status": "ok", "service": "public-cloud-stub"})
            return
        if parsed.path == _CONTROL_CREATE_PATH:
            self._handle_create_endpoint()
            return
        if parsed.path == _EMAIL_INBOX_DEFAULT_PATH:
            self._handle_get_default_email_inbox()
            return
        if parsed.path == _EMAIL_SEND_PATH:
            self._handle_send_email()
            return
        if parsed.path == _EMAIL_NEXT_PATH:
            self._handle_next_email()
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

    def _handle_get_default_email_inbox(self) -> None:
        if self.command != "POST":
            self._send_json_error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "Use POST to fetch the default email inbox.",
            )
            return
        if not self._is_authorized():
            return

        self._read_request_body()
        inbox = self.server.app.store.default_inbox()
        self._send_json(
            {
                "address": inbox.address,
                "mailbox": inbox.mailbox,
                "transport": "cloud",
            }
        )

    def _handle_send_email(self) -> None:
        if self.command != "POST":
            self._send_json_error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "Use POST to send email.",
            )
            return
        if not self._is_authorized():
            return

        payload = self._read_json_body()
        if payload is None:
            return

        subject = payload.get("subject")
        message_id = payload.get("message_id")
        if not isinstance(subject, str) or not subject.strip():
            self._send_json_error(HTTPStatus.BAD_REQUEST, "Email subject must be a non-blank string.")
            return
        if not isinstance(message_id, str) or not message_id.strip():
            self._send_json_error(HTTPStatus.BAD_REQUEST, "Email message_id must be a non-blank string.")
            return

        to_value = payload.get("to")
        if to_value is None:
            to = None
        elif isinstance(to_value, list) and all(isinstance(item, str) and item for item in to_value):
            to = list(to_value)
        else:
            self._send_json_error(
                HTTPStatus.BAD_REQUEST,
                "Email recipients must be a list of non-blank strings.",
            )
            return

        text_body = payload.get("text_body")
        html_body = payload.get("html_body")
        if text_body is not None and not isinstance(text_body, str):
            self._send_json_error(HTTPStatus.BAD_REQUEST, "text_body must be a string or null.")
            return
        if html_body is not None and not isinstance(html_body, str):
            self._send_json_error(HTTPStatus.BAD_REQUEST, "html_body must be a string or null.")
            return
        if text_body is None and html_body is None:
            self._send_json_error(HTTPStatus.BAD_REQUEST, "Provide text_body or html_body.")
            return

        from_address = payload.get("from_address")
        if from_address is not None and (
            not isinstance(from_address, str) or not from_address.strip()
        ):
            self._send_json_error(
                HTTPStatus.BAD_REQUEST,
                "from_address must be a non-blank string or null.",
            )
            return

        receipt = self.server.app.store.send_email(
            to=to,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            from_address=from_address,
            message_id=message_id,
        )
        self._send_json(receipt)

    def _handle_next_email(self) -> None:
        if self.command != "POST":
            self._send_json_error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "Use POST to fetch queued email.",
            )
            return
        if not self._is_authorized():
            return

        payload = self._read_json_body()
        if payload is None:
            return

        subject_contains = payload.get("subject_contains")
        if subject_contains is not None and not isinstance(subject_contains, str):
            self._send_json_error(
                HTTPStatus.BAD_REQUEST,
                "subject_contains must be a string or null.",
            )
            return
        from_address = payload.get("from_address")
        if from_address is not None and not isinstance(from_address, str):
            self._send_json_error(
                HTTPStatus.BAD_REQUEST,
                "from_address must be a string or null.",
            )
            return
        to_address = payload.get("to_address")
        if to_address is not None and not isinstance(to_address, str):
            self._send_json_error(
                HTTPStatus.BAD_REQUEST,
                "to_address must be a string or null.",
            )
            return
        unread_only = payload.get("unread_only", True)
        if not isinstance(unread_only, bool):
            self._send_json_error(
                HTTPStatus.BAD_REQUEST,
                "unread_only must be a boolean.",
            )
            return

        next_message = self.server.app.store.next_email(
            subject_contains=subject_contains,
            from_address=from_address,
            to_address=to_address,
            unread_only=unread_only,
        )
        if next_message is None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self._send_json(next_message)

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
            default_email_address=stub.default_inbox.address,
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


def _default_email_address(api_key: str) -> str:
    local = "".join(
        char if char.isalnum() else "_"
        for char in api_key.strip()
    ).strip("_")
    return f"{local or 'journey'}@journey-cloud.test"
