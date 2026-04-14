"""Tiny HTTP app that persists a counter in Postgres."""

from __future__ import annotations

import json
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import psycopg

APP_PORT = int(os.environ.get("APP_PORT", "8000"))
DATABASE_URL = os.environ["DATABASE_URL"]


def _connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, autocommit=True)


def _initialize_database(timeout_seconds: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with _connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS counter_state (
                            id INTEGER PRIMARY KEY,
                            count INTEGER NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        """
                        INSERT INTO counter_state (id, count)
                        VALUES (1, 0)
                        ON CONFLICT (id) DO NOTHING
                        """
                    )
            print("Journey counter app ready", flush=True)
            return
        except Exception as exc:  # pragma: no cover - only exercised in Docker
            last_error = exc
            print(f"Waiting for Postgres: {exc}", flush=True)
            time.sleep(1)
    raise RuntimeError("Postgres did not become ready before the app timeout.") from last_error


def _database_ready() -> bool:
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    return True


def _get_count() -> int:
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count FROM counter_state WHERE id = 1")
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Counter row was missing from Postgres.")
    return int(row[0])


def _increment_count() -> tuple[int, int]:
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE counter_state
                SET count = count + 1
                WHERE id = 1
                RETURNING count
                """
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Counter increment did not return a row.")
    after = int(row[0])
    return after - 1, after


class CounterHandler(BaseHTTPRequestHandler):
    server_version = "JourneyCounter/1.0"

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            _database_ready()
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "database": "ready"},
            )
            return
        if path == "/counter":
            self._send_json(
                HTTPStatus.OK,
                {"count": _get_count(), "database": "ready"},
            )
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"error": f"Unknown endpoint: {path}"},
        )

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/counter/increment":
            before, after = _increment_count()
            self._send_json(
                HTTPStatus.OK,
                {"before": before, "after": after, "database": "ready"},
            )
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"error": f"Unknown endpoint: {path}"},
        )

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)


def main() -> None:
    _initialize_database()
    server = ThreadingHTTPServer(("0.0.0.0", APP_PORT), CounterHandler)
    try:
        server.serve_forever()
    finally:  # pragma: no cover - only exercised in Docker
        server.server_close()


if __name__ == "__main__":
    main()
