"""Local bridge server used by the TypeScript Pi extension.

Threaded core (``socketserver.ThreadingTCPServer``) with thin ``async`` shims
on ``start``/``close``/``__aenter__``/``__aexit__`` for backwards compatibility
with the async ``PiPythonHarness`` and the async live tests during phases 2-3.
Phase 4 drops the shims when the harness itself is rewritten.

Per the v4 plan + v4-synthesis Tier-1 corrections, two design choices differ
from the v4 plan text:

* ``bridge_write_timeout`` uses ``select.select`` write-readiness, not
  ``socket.settimeout`` — the sidecar disconnect-watcher reads the same socket
  and would race the per-write timeout (v4 synthesis T1.4).
* Active handler threads are tracked inside ``process_request`` at the same
  point the slot semaphore is acquired and *before* the worker thread starts —
  not later in ``process_request_thread`` — so ``close()`` cannot miss a
  just-spawned handler (v4 synthesis T2.5).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import secrets
import select
import socket
import socketserver
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .jsonl import JsonlDecodeError, StrictJsonlDecoder, dumps_line
from .rpc import PiRpcProcessError
from .tools import (
    ImmutableRegistry,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    collect_tool_result,
    exception_to_wire,
    normalize_tool_value,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BridgeEndpoint:
    host: str
    port: int
    token: str
    timeout_ms: int = 120_000

    def env(self, *, prefix: str = "PI_PY") -> dict[str, str]:
        return {
            f"{prefix}_TOOLS_HOST": self.host,
            f"{prefix}_TOOLS_PORT": str(self.port),
            f"{prefix}_TOOLS_TOKEN": self.token,
            f"{prefix}_BRIDGE_TIMEOUT_MS": str(self.timeout_ms),
        }


class _BridgeHandler(socketserver.BaseRequestHandler):
    """Per-connection handler. Reads one request frame, dispatches, replies."""

    # Set in ``handle`` via attribute on ``self.server`` for typing convenience.
    server: _ThreadedBridge

    def handle(self) -> None:
        owner = self.server.owner
        # Track this socket so ``_close_sync`` can wake handlers blocked on it
        # (initial recv, sidecar recv, blocked write). F5 review fix.
        with owner._handler_sockets_lock:
            owner._active_handler_sockets.add(self.request)
        try:
            self._handle_inner(owner)
        finally:
            with owner._handler_sockets_lock:
                owner._active_handler_sockets.discard(self.request)

    def _handle_inner(self, owner: PythonToolServer) -> None:
        # T1.3 (preserved from v3): bounded read for the initial frame only.
        # Floor at 1 second so callers that pass small ``timeout_ms`` don't
        # spuriously time out before completing the handshake.
        read_timeout = max(1.0, owner.timeout_ms / 1000.0)
        self.request.settimeout(read_timeout)

        request_obj: dict[str, Any] | None = None
        in_band_violation_bytes: bytes = b""
        try:
            request_obj, in_band_violation_bytes = self._read_request_frame(max_bytes=8 * 1024 * 1024)
        except TimeoutError:
            self._write_error(None, f"bridge request timeout after {read_timeout}s")
            return
        except (ToolError, JsonlDecodeError) as exc:
            # F2 review fix: catch JsonlDecodeError (ValueError subclass) here so
            # malformed JSON returns a structured error frame instead of EOF.
            self._write_error(None, str(exc))
            return
        except OSError as exc:
            # Connection lost before the request frame arrived. No reply
            # possible; log and return.
            logger.debug("bridge connection lost before request frame: %s", exc)
            return
        finally:
            # Restore blocking semantics for tool execution and the sidecar.
            with contextlib.suppress(OSError):
                self.request.settimeout(None)

        if not hmac.compare_digest(str(request_obj.get("token", "")), owner.token):
            self._write_error(request_obj, "invalid bridge token")
            return

        try:
            self._dispatch(request_obj, in_band_violation_bytes=in_band_violation_bytes)
        except ToolError as exc:
            self._write_error(request_obj, str(exc))
        except Exception as exc:  # pragma: no cover — last-resort safety net
            wire = exception_to_wire(exc)
            self._write_error(request_obj, wire["error"], wire.get("details"))

    # --- request reading ---------------------------------------------------

    def _read_request_frame(self, *, max_bytes: int) -> tuple[dict[str, Any], bytes]:
        """Read the request frame; return ``(record, any_in_band_extra_bytes)``.

        Half-duplex contract: the client sends exactly one JSON object
        followed by ``\\n`` and nothing else. Two violation shapes both
        surface as ``in_band_extra_bytes``:

        - Trailing garbage (non-JSON or partial) after the request's LF —
          held in the decoder's internal buffer.
        - One or more *complete* additional JSONL frames after the request —
          returned alongside the first record by ``decoder.feed``. We accept
          the first record and re-serialize the extras as the violation
          payload, so the dispatch path can flag the violation consistently
          regardless of whether the extra bytes parsed.

        Raises ``ToolError`` only for truly malformed input (zero records
        from a recv that didn't error, or non-object first record).
        """
        decoder = StrictJsonlDecoder(max_buffer_bytes=max_bytes)
        received = 0
        while True:
            chunk = self.request.recv(4096)
            if not chunk:
                raise ToolError("incomplete bridge request")
            received += len(chunk)
            if received > max_bytes:
                raise ToolError(f"bridge request exceeded {max_bytes} bytes")
            records = decoder.feed(chunk)
            if records:
                if not isinstance(records[0], dict):
                    raise ToolError("expected a JSON object request")
                extra_bytes = bytes(decoder._buffer)  # noqa: SLF001 — controlled access
                if len(records) > 1:
                    # Re-serialize the extra records so the violation payload
                    # carries them; the dispatch path treats any non-empty
                    # in_band_extra_bytes as a half-duplex violation.
                    for extra_record in records[1:]:
                        extra_bytes += dumps_line(extra_record)
                return records[0], extra_bytes

    # --- dispatch ----------------------------------------------------------

    def _dispatch(self, request: dict[str, Any], *, in_band_violation_bytes: bytes = b"") -> None:
        owner = self.server.owner
        req_type = request.get("type")
        if req_type == "manifest":
            assert owner.frozen_registry is not None  # set in _start_sync
            # F16: flag protocolViolation on manifest too so the half-duplex
            # contract is enforced consistently across request types.
            if in_band_violation_bytes:
                logger.warning(
                    "bridge protocol violation: received %r after manifest request frame",
                    in_band_violation_bytes[:32],
                )
            self._write_response(
                request,
                success=True,
                data=owner.frozen_registry.manifest(),
                protocol_violation=bool(in_band_violation_bytes),
            )
            return
        if req_type != "execute":
            raise ToolError(f"unknown bridge request type {req_type!r}")

        self._handle_execute(request, in_band_violation_bytes=in_band_violation_bytes)

    def _handle_execute(self, request: dict[str, Any], *, in_band_violation_bytes: bytes = b"") -> None:
        owner = self.server.owner
        assert owner.frozen_registry is not None
        tool_name = str(request.get("tool", ""))
        tool_call_id = str(request.get("toolCallId") or request.get("id") or "python-tool-call")
        # F8 review fix: do NOT collapse falsey non-dict values to {}. Missing
        # or explicit None is fine; any other non-dict is malformed.
        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ToolError("execute.params must be an object")
        registered = owner.frozen_registry.get(tool_name)

        cancelled = threading.Event()
        protocol_violation = threading.Event()

        # In-band protocol violation: extra bytes arrived in the same recv as
        # the request frame (after its LF terminator). Flag immediately; the
        # tool may still run but the response carries the violation marker.
        if in_band_violation_bytes:
            protocol_violation.set()
            cancelled.set()
            logger.warning(
                "bridge protocol violation: received %r after request frame; "
                "cancelling tool and flagging response",
                in_band_violation_bytes[:32],
            )

        def send_update(value: ToolResult | Mapping[str, Any] | str) -> None:
            # ``ctx.update`` (currently ``async def``) calls this synchronously
            # and only awaits if we return an awaitable. Returning ``None`` is
            # the fast path — the handler thread does the actual write here.
            if cancelled.is_set():
                # Tool may keep producing updates after cancellation; drop
                # quietly. The plan calls this out under Phase 2 § Final
                # response after cancellation (F.9).
                return
            result = normalize_tool_value(value).to_wire()
            frame = dumps_line({"id": request.get("id"), "type": "update", "data": result})
            self._send_with_optional_timeout(frame)

        def watch_disconnect() -> None:
            # Half-duplex contract: after the request frame, the client must
            # send nothing else. A clean ``b""`` indicates client disconnect
            # (cancel); any other byte is a protocol violation (cancel +
            # flag + warning log per T1.4 v3).
            try:
                data = self.request.recv(1)
                if data == b"":
                    pass
                else:
                    protocol_violation.set()
                    logger.warning(
                        "bridge protocol violation: received %r after request frame; "
                        "cancelling tool and flagging response",
                        data,
                    )
            except OSError:
                # Socket closed (normal: handler ``finally`` shuts down RD).
                pass
            finally:
                cancelled.set()

        ctx = ToolContext(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            cwd=request.get("cwd"),
            metadata=dict(request.get("context") or {}),
            _cancelled=cancelled,
            _update_callback=send_update,
        )

        watcher = threading.Thread(target=watch_disconnect, daemon=True, name="bridge-watch-disconnect")
        watcher.start()

        # F9 review fix: invoke ``registered.call`` INSIDE the per-execute
        # event loop so sync tool bodies that use ``asyncio.get_event_loop()``
        # still observe a loop (matches pre-rewrite behavior).
        async def _run_tool() -> ToolResult:
            raw = registered.call(params, ctx)
            return await collect_tool_result(raw, ctx)

        try:
            result = asyncio.run(_run_tool())
            self._drain_late_violation_byte(protocol_violation, cancelled)
            self._write_response(
                request,
                success=True,
                data=result.to_wire(),
                protocol_violation=protocol_violation.is_set(),
            )
        except asyncio.CancelledError:
            # F1 review fix: ``CancelledError`` inherits from ``BaseException``
            # in 3.8+. ``collect_tool_result`` raises it on sync/async-gen
            # cancellation. Without this branch the handler would crash and
            # the client would see EOF instead of a structured response.
            self._drain_late_violation_byte(protocol_violation, cancelled)
            self._write_response(
                request,
                success=False,
                error="tool execution cancelled",
                protocol_violation=protocol_violation.is_set(),
            )
        except Exception as exc:
            self._drain_late_violation_byte(protocol_violation, cancelled)
            if cancelled.is_set():
                self._write_response(
                    request,
                    success=False,
                    error="tool execution cancelled",
                    protocol_violation=protocol_violation.is_set(),
                )
            else:
                wire = exception_to_wire(exc)
                self._write_response(
                    request,
                    success=False,
                    error=wire["error"],
                    data=wire.get("details"),
                )
        finally:
            # Interrupt the sidecar's ``recv(1)`` so we can join it. Shutting
            # only the read side avoids disrupting any in-flight final write.
            with contextlib.suppress(OSError):
                self.request.shutdown(socket.SHUT_RD)
            watcher.join(timeout=1.0)

    # Drain window: small positive timeout gives a recently-sent violation
    # byte time to propagate from the client's kernel buffer into the
    # server-side recv buffer. Sub-ms on fast loopback; up to ~10 ms under
    # scheduler contention. Tested with 30 ms; flake-free across 6 runs.
    _LATE_DRAIN_TIMEOUT_S = 0.03

    def _drain_late_violation_byte(
        self, protocol_violation: threading.Event, cancelled: threading.Event,
    ) -> None:
        """Synchronously check for a protocol-violation byte the watcher hasn't seen yet.

        F4 review fix: the watcher thread sets the violation flag
        asynchronously. If the byte arrived after tool completion but before
        the watcher's ``recv(1)`` scheduled, the response would be written
        without the ``protocolViolation`` flag (but the warning would still
        log a moment later). A short positive-timeout drain reliably catches
        the byte without unbounded latency on the fast path.

        Tradeoff: every response path pays up to ``_LATE_DRAIN_TIMEOUT_S`` of
        latency when no byte is present. For a synchronous bridge with
        per-tool human-scale latency budgets, this is negligible.
        """
        if protocol_violation.is_set():
            return
        try:
            rready, _, _ = select.select([self.request], [], [], self._LATE_DRAIN_TIMEOUT_S)
            if not rready:
                return
            data = self.request.recv(64)
            if not data:
                return
            protocol_violation.set()
            cancelled.set()
            logger.warning(
                "bridge protocol violation: received %r after request frame; "
                "flagging response (late-drain path)",
                data[:32],
            )
        except OSError:
            pass

    # --- writes ------------------------------------------------------------

    def _send_with_optional_timeout(self, frame: bytes) -> None:
        owner = self.server.owner
        timeout = owner.bridge_write_timeout
        if timeout is None:
            try:
                self.request.sendall(frame)
            except OSError as exc:
                raise PiRpcProcessError(f"bridge write failed: {exc}") from exc
            return
        # T1.4-v4: ``select.select`` write-readiness instead of socket
        # ``settimeout`` (which would race the sidecar's ``recv(1)``).
        # The middle element of the return tuple is the write-ready list.
        _, wready, _ = select.select([], [self.request], [], timeout)
        if not wready:
            raise PiRpcProcessError(f"ctx.update timeout after {timeout}s")
        try:
            self.request.sendall(frame)
        except OSError as exc:
            raise PiRpcProcessError(f"bridge write failed: {exc}") from exc

    def _write_response(
        self,
        request: dict[str, Any] | None,
        *,
        success: bool,
        data: Any = None,
        error: str | None = None,
        protocol_violation: bool = False,
    ) -> None:
        frame: dict[str, Any] = {
            "id": request.get("id") if request else None,
            "type": "response",
            "success": success,
        }
        if success:
            frame["data"] = data
        else:
            frame["error"] = error or "bridge request failed"
            if data is not None:
                frame["data"] = data
        if protocol_violation:
            frame["protocolViolation"] = True
        self._safe_sendall(dumps_line(frame))

    def _write_error(self, request: dict[str, Any] | None, error: str, data: Any = None) -> None:
        self._write_response(request, success=False, error=error, data=data)

    def _safe_sendall(self, payload: bytes) -> None:
        try:
            self.request.sendall(payload)
        except OSError as exc:
            logger.debug("bridge final write failed (client gone): %s", exc)


class _ThreadedBridge(socketserver.ThreadingTCPServer):
    """``ThreadingTCPServer`` with handler-slot gating and thread tracking.

    The gating happens in ``process_request`` (where the worker thread is
    created), not in ``process_request_thread`` (which runs *on* the worker
    thread). This bounds *thread creation*, not just execution — closing the
    v3 plan's "limits execution but not thread creation" gap.

    Tracking the thread in the same method that creates it (T2.5-v4) means
    ``close()`` cannot snapshot ``_active_handler_threads`` in the window
    between thread creation and ``process_request_thread`` entry.
    """

    daemon_threads = True
    allow_reuse_address = True
    # ``ThreadingMixIn.server_close`` joins ``self._threads`` if non-None.
    # We do our own bounded join in ``PythonToolServer._close_sync`` to avoid
    # unbounded blocking on uncooperative tool bodies. Don't accumulate.
    block_on_close = False

    def __init__(self, addr: tuple[str, int], handler_cls: type, *, owner: PythonToolServer) -> None:
        super().__init__(addr, handler_cls)
        self.owner = owner

    def process_request(self, request: Any, client_address: Any) -> None:
        owner = self.owner
        if not owner._handler_slots.acquire(blocking=False):
            logger.warning("bridge at capacity (max_handlers=%d); rejecting connection", owner.max_handlers)
            with contextlib.suppress(OSError):
                self.shutdown_request(request)
            return
        # F7 review fix: wrap thread construction in the same try as start().
        # If ``Thread(...)`` raises (MemoryError under pressure, etc.), the
        # previous code leaked the semaphore slot.
        slot_released = False
        t: threading.Thread | None = None
        try:
            t = threading.Thread(
                target=self.process_request_thread,
                args=(request, client_address),
                name="bridge-handler",
                daemon=self.daemon_threads,
            )
            with owner._handler_threads_lock:
                owner._active_handler_threads.add(t)
            t.start()
        except BaseException:
            if t is not None:
                with owner._handler_threads_lock:
                    owner._active_handler_threads.discard(t)
            if not slot_released:
                owner._handler_slots.release()
                slot_released = True
            with contextlib.suppress(OSError):
                self.shutdown_request(request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        owner = self.owner
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            with contextlib.suppress(OSError):
                self.shutdown_request(request)
            with owner._handler_threads_lock:
                owner._active_handler_threads.discard(threading.current_thread())
            owner._handler_slots.release()


class PythonToolServer:
    """Token-protected localhost JSONL bridge for Python tools.

    Sync threaded core; ``start``/``close``/``__aenter__``/``__aexit__`` are
    async-shim wrappers for backwards compatibility with the async harness
    during phases 2-3. The wrappers do no actual ``await`` work — they call
    the sync core methods directly — and will be removed in phase 4 when the
    harness is rewritten.

    Slowloris note: ``timeout_ms`` is a per-recv timeout. An attacker that
    dribbles bytes can hold a handler slot until the 8 MiB byte cap fires.
    Default ``max_handlers=256`` is the slot ceiling and is also the
    slowloris-DoS surface; tune both knobs together for hostile clients.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
        timeout_ms: int = 120_000,
        max_handlers: int = 256,
        bridge_write_timeout: float | None = None,
    ) -> None:
        """Construct the server.

        ``bridge_write_timeout`` is **best-effort, pre-write only**. We call
        ``select.select`` with this timeout before each ``ctx.update``
        ``sendall``; if the socket isn't write-ready, we raise. Once
        ``sendall`` starts, it can still block if the peer drains slowly
        (large frames + small kernel buffers). For full per-write
        deadlines, use OS-level mitigations (process isolation, separate
        listener). Final response writes are unbounded by design — they're
        short and the peer is expected to consume promptly.
        """
        self.registry = registry
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self.timeout_ms = timeout_ms
        self.max_handlers = max_handlers
        self.bridge_write_timeout = bridge_write_timeout

        self.frozen_registry: ImmutableRegistry | None = None
        self._http_server: _ThreadedBridge | None = None
        self._serve_thread: threading.Thread | None = None
        self._serve_entered = threading.Event()  # F3: set by serve target
        self._handler_slots = threading.Semaphore(max_handlers)
        self._active_handler_threads: set[threading.Thread] = set()
        self._handler_threads_lock = threading.Lock()
        # F5 review fix: track accepted sockets so close() can wake handlers.
        self._active_handler_sockets: set[socket.socket] = set()
        self._handler_sockets_lock = threading.Lock()
        # F11 review fix: serialize concurrent close calls.
        self._close_lock = threading.Lock()

    @property
    def endpoint(self) -> BridgeEndpoint:
        if self._http_server is None:
            raise RuntimeError("PythonToolServer is not started")
        host, port = self._http_server.server_address[:2]
        return BridgeEndpoint(host=str(host), port=int(port), token=self.token, timeout_ms=self.timeout_ms)

    # --- sync core ---------------------------------------------------------

    def _start_sync(self) -> BridgeEndpoint:
        if self._http_server is not None:
            return self.endpoint
        # Bind to a private snapshot of the registry — caller's registry is
        # never mutated, and late ``register()`` calls don't leak into the
        # running server (T2.6-v4).
        self.frozen_registry = self.registry.snapshot()
        # F3 review fix: keep the server in a local until the serve thread is
        # successfully running. If anything fails, close it directly and
        # leave ``_http_server`` as None so a subsequent ``_close_sync`` is a
        # safe no-op.
        srv = _ThreadedBridge((self.host, self.port), _BridgeHandler, owner=self)
        self._serve_entered.clear()

        def _serve_target() -> None:
            self._serve_entered.set()
            srv.serve_forever()

        serve_thread = threading.Thread(target=_serve_target, name="pi-bridge-server", daemon=True)
        try:
            serve_thread.start()
        except BaseException:
            with contextlib.suppress(Exception):
                srv.server_close()
            raise
        self._http_server = srv
        self._serve_thread = serve_thread
        return self.endpoint

    def _close_sync(self) -> None:
        # F11 review fix: serialize concurrent close.
        with self._close_lock:
            srv = self._http_server
            serve_thread = self._serve_thread
            self._http_server = None
            self._serve_thread = None
        if srv is None and serve_thread is None:
            return

        # F3 review fix: only call shutdown() if the serve thread actually
        # entered serve_forever(); otherwise ``shutdown()`` would wait forever
        # for an ``__is_shut_down`` event the serve loop never gets to set.
        if srv is not None:
            if self._serve_entered.wait(timeout=2.0):
                with contextlib.suppress(Exception):
                    srv.shutdown()
            with contextlib.suppress(Exception):
                srv.server_close()
        if serve_thread is not None:
            serve_thread.join(timeout=2.0)

        # F5 review fix: wake any handler blocked on its accepted socket
        # BEFORE joining handler threads. Most cooperative handlers exit
        # within tens of ms after their socket is shut down.
        with self._handler_sockets_lock:
            sockets = list(self._active_handler_sockets)
        for s in sockets:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)

        # F6 review fix: total-deadline join across all handlers (not 2 s
        # each). ``daemon_threads = True`` is the process-exit safety net for
        # whatever doesn't finish in time.
        with self._handler_threads_lock:
            handlers = list(self._active_handler_threads)
        # F10 review fix: skip self if a tool body calls close().
        current = threading.current_thread()
        deadline = time.monotonic() + 3.0
        for t in handlers:
            if t is current:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(timeout=remaining)

    # --- async shims (phases 2-3) -----------------------------------------

    async def start(self) -> BridgeEndpoint:
        return self._start_sync()

    async def close(self) -> None:
        self._close_sync()

    async def __aenter__(self) -> PythonToolServer:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()
