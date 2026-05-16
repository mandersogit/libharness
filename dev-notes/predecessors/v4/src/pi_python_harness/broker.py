from __future__ import annotations

import asyncio
import json
import secrets
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import urlparse

from .jsonl import dumps_line
from .tools import ToolRegistry, normalize_tool_result
from .types import ToolCallContext, ToolResult


@dataclass(frozen=True)
class BrokerHandle:
    url: str
    token: str


class PythonToolBroker:
    """Token-protected localhost HTTP/NDJSON tool broker.

    The TypeScript shim calls `/execute` for each Pi tool invocation. The response
    is newline-delimited JSON so tools can stream progress updates before a final
    result.
    """

    def __init__(self, registry: ToolRegistry, *, host: str = "127.0.0.1", port: int = 0, token: str | None = None) -> None:
        self.registry = registry
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def handle(self) -> BrokerHandle:
        if self._server is None:
            raise RuntimeError("Broker has not been started")
        host, port = self._server.server_address[:2]
        return BrokerHandle(url=f"http://{host}:{port}", token=self.token)

    def start(self) -> BrokerHandle:
        if self._server is not None:
            return self.handle

        broker = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "PiPythonToolBroker/0.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - inherited API name
                return

            def _json_response(self, status: int, body: Mapping[str, Any]) -> None:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _check_token(self) -> bool:
                supplied = self.headers.get("X-Pi-Python-Token", "")
                return secrets.compare_digest(supplied, broker.token)

            def do_GET(self) -> None:  # noqa: N802 - stdlib API
                path = urlparse(self.path).path
                if path == "/healthz":
                    self._json_response(HTTPStatus.OK, {"ok": True})
                    return
                if path == "/manifest":
                    if not self._check_token():
                        self._json_response(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json_response(HTTPStatus.OK, broker.registry.manifest())
                    return
                self._json_response(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802 - stdlib API
                path = urlparse(self.path).path
                if path == "/session_shutdown":
                    self._json_response(HTTPStatus.OK, {"ok": True})
                    return
                if path != "/execute":
                    self._json_response(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if not self._check_token():
                    self._json_response(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return

                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    if not isinstance(payload, dict):
                        raise ValueError("request body must be a JSON object")
                    tool_name = str(payload["toolName"])
                    tool_call_id = str(payload.get("toolCallId", ""))
                    arguments = payload.get("arguments", {})
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be a JSON object")
                except Exception as exc:
                    self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return

                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

                def emit(record: Mapping[str, Any]) -> None:
                    self.wfile.write(dumps_line(record))
                    self.wfile.flush()

                def emit_update(value: ToolResult | str | Mapping[str, Any]) -> None:
                    emit({"type": "update", "result": normalize_tool_result(value).to_json()})

                ctx = ToolCallContext(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    cwd=payload.get("cwd"),
                    pi_context=payload.get("context") if isinstance(payload.get("context"), dict) else {},
                    update=emit_update,
                )

                try:
                    result = asyncio.run(broker.registry.invoke(tool_name, arguments, ctx))
                    emit({"type": "result", "result": result.to_json()})
                except Exception as exc:
                    emit({"type": "error", "message": str(exc)})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="pi-python-tool-broker", daemon=True)
        self._thread.start()
        return self.handle

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    def __enter__(self) -> "PythonToolBroker":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop()
