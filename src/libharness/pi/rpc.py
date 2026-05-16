"""Subprocess client for ``pi --mode rpc``.

Sync threaded core; ``async def`` shims preserved on the public surface so
the still-async ``PiPythonHarness`` and live tests stay green during the
phase 2-3 transition. Phase 4 drops the async shims.

Key design choices (per v4 plan § 3 + § 6, with the v4-synthesis Tier-1
corrections):

* **UI response priority via literal v3-synthesis pattern.** ``send()``
  acquires ``_send_lock`` FIRST, then re-checks ``_ui_response_pending``;
  if set, releases the lock and waits on ``_ui_response_condition``,
  retries. NOT v4's "layered locking" (the v4 synthesis caught that as a
  re-introduction of the v3 post-gate race). Reader-owned UI response
  writes bypass the condition.
* **Future settlement via two helpers, not one polymorphic helper.**
  ``_pop_pending(req_id)`` (response path) + ``_complete_future(fut, ...)``
  (owner-has-future path). The single ``_settle_future`` in v3 had
  ambiguous ownership semantics; T1.2-v4 split closes it.
* **``close()`` postcondition: "no NEW callbacks after close begins."** In-
  flight callbacks may continue running but observe ``_closing`` and fail
  any subsequent client access. The v4 plan's stronger "no callbacks after
  close returns" was unimplementable (T1.3-v4).
* **Update wire shape preserved**: ``{"id": req_id, "type": "update",
  "data": ...}``. v4's pseudocode dropped the ``id`` and renamed ``data``
  to ``result`` — T2.2-v4 caught this would break ``shim.py``.
* **New exception hierarchy.** ``PiRpcError`` is the abstract base.
  Subclasses: ``PiRpcCommandError`` (command returned ``success=False``),
  ``PiRpcProcessError`` (subprocess / reader / close failure),
  ``ReentrantRPCError`` (same-thread ``send`` from inside a handler),
  ``EventQueueEmpty`` (replaces ``queue.Empty`` at the public boundary).
"""

from __future__ import annotations

import collections.abc
import contextlib
import inspect
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Future, InvalidStateError
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jsonl import StrictJsonlDecoder, dumps_line

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]
EventHandler = Callable[[JsonObject], None | Awaitable[None]]
UiHandler = Callable[[JsonObject], JsonObject | None | Awaitable[JsonObject | None]]

DEFAULT_EVENT_QUEUE_MAX = 4096


class PiRpcError(RuntimeError):
    """Abstract base for all RPC client errors.

    Catching ``PiRpcError`` catches every concrete subclass via the base —
    callers that did ``except PiRpcError:`` for command failures keep
    working after the v4 hierarchy reshuffle.
    """


class PiRpcCommandError(PiRpcError):
    """Pi returned ``success=False`` for an RPC command.

    Was the old ``PiRpcError`` semantic (carries failed-command attrs); the
    name change keeps the public base name (``PiRpcError``) free for the
    new abstract base.
    """

    def __init__(self, command: str, error: str, response: JsonObject | None = None) -> None:
        super().__init__(f"Pi RPC command {command!r} failed: {error}")
        self.command = command
        self.error = error
        self.response = response or {}


class PiRpcProcessError(PiRpcError):
    """Subprocess can't be started / reader fatal / close failure / bridge transport."""


class ReentrantRPCError(PiRpcError):
    """``send()`` called from inside an event or UI handler on the same thread.

    Same-thread reentry would block forever (the reader thread is busy
    running the handler and can't dispatch the response). Helper-thread
    reentry (handler spawns a worker that calls ``send``) is NOT detected
    and will deadlock — documented contract violation; tested out-of-process.
    """


class EventQueueEmpty(PiRpcError):
    """``next_event(timeout=...)`` or ``wait_for_event(timeout=...)`` exceeded its budget.

    Replaces ``queue.Empty`` at the public API boundary so callers can
    ``except PiRpcError:`` consistently.
    """


def _caused(exc_cls: type[BaseException], msg: str, cause: BaseException) -> BaseException:
    """Build ``exc_cls(msg)`` with ``__cause__`` set to ``cause`` without raising.

    ``raise X from Y`` is only valid in a ``raise`` statement; this helper
    lets us construct the exception eagerly (for ``_complete_future``).
    """
    exc = exc_cls(msg)
    exc.__cause__ = cause
    return exc


@dataclass(slots=True)
class PiLaunchConfig:
    """Launch settings for a Pi RPC subprocess."""

    pi_command: str | Sequence[str] = "pi"
    cwd: str | Path | None = None
    env: Mapping[str, str] | None = None
    provider: str | None = None
    model: str | None = None
    no_session: bool = True
    offline: bool = True
    no_extensions: bool = True
    no_skills: bool = True
    no_prompt_templates: bool = True
    no_context_files: bool = True
    no_builtin_tools: bool = False
    tools: Sequence[str] | None = None
    session_dir: str | Path | None = None
    session: str | None = None
    startup_timeout: float = 5.0
    request_timeout: float = 30.0
    extra_args: Sequence[str] = field(default_factory=tuple)
    event_queue_max: int = DEFAULT_EVENT_QUEUE_MAX

    def base_argv(self) -> list[str]:
        if isinstance(self.pi_command, str):
            argv = [self.pi_command]
        else:
            argv = list(self.pi_command)
        argv.extend(["--mode", "rpc"])
        if self.provider:
            argv.extend(["--provider", self.provider])
        if self.model:
            argv.extend(["--model", self.model])
        if self.no_session:
            argv.append("--no-session")
        if self.offline:
            argv.append("--offline")
        if self.session_dir:
            argv.extend(["--session-dir", str(self.session_dir)])
        if self.session:
            argv.extend(["--session", self.session])
        if self.no_extensions:
            argv.append("--no-extensions")
        if self.no_skills:
            argv.append("--no-skills")
        if self.no_prompt_templates:
            argv.append("--no-prompt-templates")
        if self.no_context_files:
            argv.append("--no-context-files")
        if self.no_builtin_tools:
            argv.append("--no-builtin-tools")
        if self.tools is not None:
            argv.extend(["--tools", ",".join(self.tools)])
        argv.extend(self.extra_args)
        return argv


# UI request types that pi expects to be ignored (notification-only, no response).
_NOTIFICATION_UI_METHODS = frozenset({"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"})


class PiRpcClient:
    """Threaded Python client for Pi's LF-delimited JSON RPC mode.

    Public methods are ``async def`` shims wrapping a sync core (the
    ``_*_sync`` variants). Phase 4 will drop the async shims and rename the
    sync core to the public names. Until then both surfaces are live: the
    sync core works directly from any thread, and the async shims are
    callable from asyncio code (they block the event loop while the sync
    core does its work, which is acceptable for the test harness).
    """

    def __init__(self, config: PiLaunchConfig | None = None) -> None:
        self.config = config or PiLaunchConfig()
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout_reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

        # Pending requests + fatal state (one lock guards both for atomicity).
        self._pending_lock = threading.Lock()
        self._pending: dict[str, Future[JsonObject]] = {}
        self._fatal_error: BaseException | None = None

        # Stdin write serialization + invariants.
        self._send_lock = threading.Lock()
        self._stdin_closed = False  # guarded by _send_lock

        # Lifecycle.
        self._closing = threading.Event()
        self._close_complete = threading.Event()
        self._close_lock = threading.Lock()  # serializes close()

        # Event delivery.
        self._events: queue.Queue[JsonObject] = queue.Queue(maxsize=self.config.event_queue_max)
        self._events_drop_lock = threading.Lock()
        self._dropped_event_count = 0
        self._last_drop_warning_time = 0.0

        # Event/UI handler registries (lock guards mutation + snapshot).
        self._handlers_lock = threading.Lock()
        self._event_handlers: list[EventHandler] = []
        self._ui_handlers: dict[str, UiHandler] = {}
        self._fallback_ui_handler: UiHandler | None = None

        # UI response priority (T1.1 literal v3-synthesis pattern).
        self._ui_response_pending = threading.Event()
        self._ui_response_condition = threading.Condition()

        # Reentrant-dispatch detection.
        self._in_dispatch = threading.local()

        # Stderr accumulation (no lock — only the stderr-reader appends).
        self._stderr_chunks: list[str] = []
        self._request_counter = 0

    # --- introspection -----------------------------------------------------

    @property
    def stderr(self) -> str:
        return "".join(self._stderr_chunks)

    def argv(self, *, extension_paths: Sequence[str | Path] = ()) -> list[str]:
        argv = self.config.base_argv()
        for extension_path in extension_paths:
            argv.extend(["--extension", str(extension_path)])
        return argv

    # --- handler registration (sync; safe from any thread) ----------------

    def on_event(self, handler: EventHandler) -> Callable[[], None]:
        if inspect.iscoroutinefunction(handler) or inspect.isasyncgenfunction(handler):
            raise TypeError(
                "async event handlers are not supported; see docs/DESIGN.md § Breaking changes",
            )
        with self._handlers_lock:
            self._event_handlers.append(handler)

        def unsubscribe() -> None:
            with self._handlers_lock, contextlib.suppress(ValueError):
                self._event_handlers.remove(handler)

        return unsubscribe

    def set_extension_ui_handler(self, method: str | None, handler: UiHandler | None) -> None:
        if handler is not None and (
            inspect.iscoroutinefunction(handler) or inspect.isasyncgenfunction(handler)
        ):
            raise TypeError(
                "async UI handlers are not supported; see docs/DESIGN.md § Breaking changes",
            )
        with self._handlers_lock:
            if method is None:
                self._fallback_ui_handler = handler
            elif handler is None:
                self._ui_handlers.pop(method, None)
            else:
                self._ui_handlers[method] = handler

    # --- sync core: lifecycle ---------------------------------------------

    def _start_sync(self, *, extension_paths: Sequence[str | Path] = ()) -> None:
        if self.process is not None:
            raise PiRpcProcessError("Pi RPC client is already started")
        # F6 review fix: restart is unsupported — _fatal_error is not reset
        # by _close_sync, so a restart would launch a new subprocess that's
        # immediately fatal. Make it explicit per the v4 plan's
        # "PiRpcClient is single-use" stance.
        if self._close_complete.is_set():
            raise RuntimeError(
                "PiRpcClient is single-use; create a new instance to start again",
            )
        argv = self.argv(extension_paths=extension_paths)
        env = os.environ.copy()
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        if self.config.offline:
            env.setdefault("PI_OFFLINE", "1")
        if self.config.env:
            env.update(dict(self.config.env))
        try:
            self.process = subprocess.Popen(
                argv,
                cwd=str(self.config.cwd) if self.config.cwd is not None else None,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise PiRpcProcessError(f"could not start Pi command {argv[0]!r}") from exc

        # NOTE: do NOT clear _closing / _close_complete here. Restart is
        # rejected above (F6); these events are write-once for a given
        # instance's lifetime.
        self._stdout_reader = threading.Thread(target=self._read_stdout_loop, name="pi-rpc-stdout", daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr_loop, name="pi-rpc-stderr", daemon=True)
        self._stdout_reader.start()
        self._stderr_reader.start()

        # Brief startup wait so subprocess crashes during launch surface as
        # PiRpcProcessError rather than later send timeouts.
        startup_wait = min(0.2, max(0.0, self.config.startup_timeout))
        if startup_wait > 0:
            time_start = _monotonic()
            while _monotonic() - time_start < startup_wait:
                if self.process.poll() is not None:
                    break
                _sleep(0.02)
        rc = self.process.poll()
        if rc is not None:
            # F8 review fix: clean up the failed subprocess + reader threads
            # before raising. Otherwise the caller is left with a poisoned
            # client (process handle present, reader threads live, stderr
            # accumulating to /dev/null).
            stderr_snapshot = self.stderr
            with contextlib.suppress(Exception):
                if self.process.stdin is not None:
                    self.process.stdin.close()
            with contextlib.suppress(Exception):
                self.process.wait(timeout=1.0)
            for t in (self._stdout_reader, self._stderr_reader):
                if t is not None:
                    t.join(timeout=1.0)
            self._stdout_reader = None
            self._stderr_reader = None
            self.process = None
            # Mark fatal so any pending sends (shouldn't be any pre-handshake)
            # get the right error, and so re-entering start raises cleanly.
            self._closing.set()
            self._close_complete.set()
            raise PiRpcProcessError(
                f"Pi exited during startup with code {rc}. stderr={stderr_snapshot!r}",
            )

    def _close_sync(self) -> None:
        with self._close_lock:
            if self._close_complete.is_set():
                return
            if self._closing.is_set():
                # Another caller is mid-close; wait for them and return.
                already_closing = True
            else:
                already_closing = False
                self._closing.set()

        if already_closing:
            self._close_complete.wait(timeout=10.0)
            return

        try:
            # 1. Atomic fatal: any in-flight send() observes process error.
            self._set_fatal(PiRpcProcessError("Pi RPC client closed"))

            # F3 review fix: wake any caller waiting on the UI gate BEFORE
            # any other close work so they observe _closing and bail.
            with self._ui_response_condition:
                self._ui_response_condition.notify_all()

            proc = self.process
            if proc is not None:
                # F3 review fix: try to acquire _send_lock with a SHORT
                # timeout. A caller may be blocked in proc.stdin.write/flush
                # waiting for pi to drain (if pi isn't draining stdin, that
                # caller holds _send_lock indefinitely). In that case we MUST
                # escalate terminate/kill BEFORE the lock acquire to unblock
                # the writer (BrokenPipeError on the closed stdin).
                acquired = self._send_lock.acquire(timeout=2.0)
                if not acquired:
                    # Lock blocked — force the subprocess down to unblock
                    # whoever holds the lock. The escalation walks
                    # terminate → wait(1.0) → kill → wait(1.0).
                    logger.warning(
                        "close: _send_lock blocked (caller stuck in stdin.write); "
                        "escalating terminate/kill to unblock",
                    )
                    self._force_terminate_subprocess(proc)
                    # Now retry the lock; the writer should have released
                    # with BrokenPipeError (caught + raised as PiRpcProcessError).
                    acquired = self._send_lock.acquire(timeout=1.0)
                try:
                    self._stdin_closed = True
                    if proc.stdin is not None and not proc.stdin.closed:
                        with contextlib.suppress(BrokenPipeError, OSError):
                            proc.stdin.close()
                finally:
                    if acquired:
                        self._send_lock.release()

                # 3. Wait for pi to exit; escalate via TERM then KILL if
                #    we didn't already escalate above.
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self._force_terminate_subprocess(proc)

            # 4. Join reader threads (skip self to avoid RuntimeError if a
            #    reader callback called close()).
            current = threading.current_thread()
            for t in (self._stdout_reader, self._stderr_reader):
                if t is not None and t is not current:
                    t.join(timeout=2.0)
            self._stdout_reader = None
            self._stderr_reader = None
            self.process = None

            # 5. Wake any caller still waiting on _ui_response_condition so
            #    they observe _closing and bail.
            with self._ui_response_condition:
                self._ui_response_condition.notify_all()
        except Exception:
            logger.exception("close: cleanup raised; final state will still be 'closed'")
        finally:
            self._close_complete.set()

    # --- sync core: send + UI priority -----------------------------------

    def _send_sync(self, command: Mapping[str, Any], *, timeout: float | None = None) -> JsonObject:
        if getattr(self._in_dispatch, "value", False):
            raise ReentrantRPCError(
                "client.send() called from inside a synchronous event/UI handler on the "
                "same thread; the reader thread is busy and cannot dispatch the response",
            )
        if self.process is None:
            raise PiRpcProcessError("Pi RPC client is not started")

        request = dict(command)
        request_id = str(request.get("id") or self._next_request_id())
        request["id"] = request_id
        fut: Future[JsonObject] = Future()

        # T1.4-v4 atomic: fatal check + pending insertion under _pending_lock.
        with self._pending_lock:
            if self._fatal_error is not None:
                raise _caused(
                    PiRpcProcessError, "rpc client is in fatal state", self._fatal_error,
                )
            self._pending[request_id] = fut

        payload = dumps_line(request)
        wait_timeout = timeout if timeout is not None else self.config.request_timeout
        try:
            # F5 review fix: thread the effective deadline through so the UI
            # gate retry honors the caller's per-call timeout, not just
            # config.request_timeout.
            self._write_stdin_normal(payload, deadline=_monotonic() + wait_timeout)
        except PiRpcProcessError as exc:
            popped = self._pop_pending(request_id)
            if popped is not None:
                self._complete_future(popped, exc=exc)
            raise

        try:
            response = fut.result(timeout=wait_timeout)
        except FutureTimeoutError as exc:
            popped = self._pop_pending(request_id)
            if popped is not None:
                self._complete_future(
                    popped,
                    exc=PiRpcProcessError(f"send timeout after {wait_timeout}s"),
                )
            raise PiRpcProcessError(f"send timeout after {wait_timeout}s") from exc

        if response.get("success") is False:
            raise PiRpcCommandError(
                str(request.get("type", "unknown")),
                str(response.get("error", "")),
                response,
            )
        return response

    def _write_stdin_normal(self, payload: bytes, *, deadline: float) -> None:
        """Normal-path stdin write with literal v3-synthesis UI priority.

        Loop: acquire ``_send_lock`` first. Under the lock, re-check the
        ``_ui_response_pending`` flag. If clear, write and return. If set,
        release the lock and wait on ``_ui_response_condition`` until the
        reader clears the flag (or close fires); retry.

        ``deadline`` is the absolute monotonic time at which the caller's
        send budget expires. The UI-gate wait is bounded by
        ``min(deadline - now, request_timeout)`` so a caller-supplied small
        timeout isn't silently extended by the config default (F5 review fix).
        """
        while True:
            with self._send_lock:
                if self._closing.is_set():
                    raise PiRpcProcessError("rpc client is closing")
                if not self._ui_response_pending.is_set():
                    if self._stdin_closed:
                        raise PiRpcProcessError("stdin closed")
                    proc = self.process
                    if proc is None or proc.stdin is None:
                        raise PiRpcProcessError("subprocess stdin is unavailable")
                    try:
                        proc.stdin.write(payload)
                        proc.stdin.flush()
                    except (BrokenPipeError, ValueError, OSError) as exc:
                        raise PiRpcProcessError("stdin write failed") from exc
                    return
            # UI gate is set; release send lock, wait, retry. Bounded by the
            # caller's deadline AND the config default.
            remaining = deadline - _monotonic()
            if remaining <= 0:
                raise PiRpcProcessError("send timeout while waiting for UI response gate")
            wait_budget = min(remaining, max(0.1, self.config.request_timeout))
            with self._ui_response_condition:
                if not self._ui_response_condition.wait_for(
                    lambda: not self._ui_response_pending.is_set() or self._closing.is_set(),
                    timeout=wait_budget,
                ):
                    # Either the wait timed out or close fired.
                    if self._closing.is_set():
                        raise PiRpcProcessError("rpc client is closing")
                    if _monotonic() >= deadline:
                        raise PiRpcProcessError("send timeout while waiting for UI response gate")
                    # Otherwise: config-level wait expired; loop and try again.
            # Loop back to retry the lock+recheck+write.

    # --- sync core: future settlement helpers (T1.2-v4) ------------------

    def _pop_pending(self, req_id: str) -> Future[JsonObject] | None:
        with self._pending_lock:
            return self._pending.pop(req_id, None)

    def _complete_future(
        self,
        fut: Future[JsonObject],
        *,
        value: JsonObject | None = None,
        exc: BaseException | None = None,
    ) -> None:
        try:
            if exc is not None:
                fut.set_exception(exc)
            else:
                # Type system can't see that value is non-None when exc is None.
                fut.set_result(value)  # type: ignore[arg-type]
        except InvalidStateError:
            # Already completed by another path; benign per v4-synthesis T1.2.
            pass

    def _force_terminate_subprocess(self, proc: subprocess.Popen[bytes]) -> None:
        """SIGTERM → wait(1) → kill → wait(1). Best-effort; never raises.

        Used by ``_close_sync`` when the normal close path stalls (e.g.,
        because a caller holds ``_send_lock`` blocked in ``proc.stdin.write``
        waiting for pi to drain — terminating pi unblocks the writer with
        ``BrokenPipeError``).
        """
        with contextlib.suppress(Exception):
            if sys.platform == "win32":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=1.0)

    def _set_fatal(self, exc: BaseException) -> None:
        """Mark the client fatal and settle all pending futures.

        Idempotent: a second call is a no-op. Snapshots ``_pending`` under
        the lock, releases, then settles. Never holds ``_pending_lock``
        across ``_complete_future`` (which can take other locks).
        """
        with self._pending_lock:
            if self._fatal_error is not None:
                return
            self._fatal_error = exc
            pending_snapshot = list(self._pending.values())
            self._pending.clear()
        for fut in pending_snapshot:
            self._complete_future(
                fut, exc=_caused(PiRpcProcessError, "rpc client fatal", exc),
            )

    # --- sync core: convenience methods ----------------------------------

    def _prompt_sync(
        self,
        message: str,
        *,
        streaming_behavior: str | None = None,
        images: list[JsonObject] | None = None,
        **extra: Any,
    ) -> JsonObject:
        request: JsonObject = {"type": "prompt", "message": message, **extra}
        if streaming_behavior is not None:
            request["streamingBehavior"] = streaming_behavior
        if images is not None:
            request["images"] = images
        return self._send_sync(request)

    def _prompt_and_wait_sync(
        self, message: str, *, timeout: float = 120.0, **kwargs: Any,
    ) -> list[JsonObject]:
        events: list[JsonObject] = []
        done = threading.Event()

        def collect(event: JsonObject) -> None:
            events.append(event)
            if event.get("type") == "agent_end":
                done.set()

        unsubscribe = self.on_event(collect)
        try:
            self._prompt_sync(message, **kwargs)
            if not done.wait(timeout=timeout):
                raise PiRpcProcessError(f"prompt_and_wait timeout after {timeout}s")
            return events
        finally:
            unsubscribe()

    def _steer_sync(self, message: str, *, images: list[JsonObject] | None = None) -> JsonObject:
        request: JsonObject = {"type": "steer", "message": message}
        if images is not None:
            request["images"] = images
        return self._send_sync(request)

    def _follow_up_sync(self, message: str, *, images: list[JsonObject] | None = None) -> JsonObject:
        request: JsonObject = {"type": "follow_up", "message": message}
        if images is not None:
            request["images"] = images
        return self._send_sync(request)

    def _abort_sync(self) -> JsonObject:
        return self._send_sync({"type": "abort"})

    def _new_session_sync(self) -> JsonObject:
        return self._send_sync({"type": "new_session"})

    def _get_state_sync(self) -> JsonObject:
        return dict(self._send_sync({"type": "get_state"}).get("data") or {})

    def _get_messages_sync(self) -> list[JsonObject]:
        data = self._send_sync({"type": "get_messages"}).get("data") or {}
        return list(data.get("messages") or [])

    def _get_commands_sync(self) -> list[JsonObject]:
        data = self._send_sync({"type": "get_commands"}).get("data") or {}
        return list(data.get("commands") or [])

    def _get_available_models_sync(self) -> list[JsonObject]:
        data = self._send_sync({"type": "get_available_models"}).get("data") or {}
        return list(data.get("models") or data.get("availableModels") or [])

    def _set_model_sync(self, provider: str, model_id: str) -> JsonObject:
        # Pi's RPC contract requires ``modelId``, not ``model`` — see
        # packages/coding-agent/src/modes/rpc/rpc-types.ts:31 in pi-mono.
        return self._send_sync({"type": "set_model", "provider": provider, "modelId": model_id})

    def _bash_sync(self, command: str) -> JsonObject:
        return dict(self._send_sync({"type": "bash", "command": command}).get("data") or {})

    def _next_event_sync(self, *, timeout: float | None = None) -> JsonObject:
        try:
            if timeout is None:
                return self._events.get()
            return self._events.get(timeout=timeout)
        except queue.Empty as exc:
            raise EventQueueEmpty(f"no event within {timeout}s") from exc

    def _wait_for_event_sync(self, event_type: str, *, timeout: float | None = None) -> JsonObject:
        # Note: per-event timeout, not total. Matches pre-rewrite semantics.
        while True:
            event = self._next_event_sync(timeout=timeout)
            if event.get("type") == event_type:
                return event

    def _get_last_assistant_text_sync(self) -> str | None:
        for message in reversed(self._get_messages_sync()):
            if message.get("role") != "assistant":
                continue
            chunks: list[str] = []
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(str(block.get("text") or ""))
            if chunks:
                return "\n".join(chunks)
        return None

    # --- async shims (phases 2-3; dropped in phase 4) ---------------------
    #
    # F7 review fix: async shims call the sync core via ``asyncio.to_thread``
    # so the asyncio event loop is not blocked for the duration of the sync
    # call. This makes ``asyncio.wait_for(client.X(...), timeout=...)`` work
    # as expected (the await point exists; cancellation can be delivered;
    # the request is NOT cancellable in the sync core, but the loop is free).
    # Phase 4 drops these wrappers entirely.

    async def start(self, *, extension_paths: Sequence[str | Path] = ()) -> None:
        import asyncio
        await asyncio.to_thread(self._start_sync, extension_paths=extension_paths)

    async def close(self) -> None:
        import asyncio
        await asyncio.to_thread(self._close_sync)

    async def __aenter__(self) -> PiRpcClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def send(self, command: Mapping[str, Any], *, timeout: float | None = None) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(self._send_sync, command, timeout=timeout)

    async def prompt(self, *args: Any, **kwargs: Any) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(lambda: self._prompt_sync(*args, **kwargs))

    async def prompt_and_wait(self, *args: Any, **kwargs: Any) -> list[JsonObject]:
        import asyncio
        return await asyncio.to_thread(lambda: self._prompt_and_wait_sync(*args, **kwargs))

    async def steer(self, *args: Any, **kwargs: Any) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(lambda: self._steer_sync(*args, **kwargs))

    async def follow_up(self, *args: Any, **kwargs: Any) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(lambda: self._follow_up_sync(*args, **kwargs))

    async def abort(self) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(self._abort_sync)

    async def new_session(self) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(self._new_session_sync)

    async def get_state(self) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(self._get_state_sync)

    async def get_messages(self) -> list[JsonObject]:
        import asyncio
        return await asyncio.to_thread(self._get_messages_sync)

    async def get_commands(self) -> list[JsonObject]:
        import asyncio
        return await asyncio.to_thread(self._get_commands_sync)

    async def get_available_models(self) -> list[JsonObject]:
        import asyncio
        return await asyncio.to_thread(self._get_available_models_sync)

    async def set_model(self, provider: str, model_id: str) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(self._set_model_sync, provider, model_id)

    async def bash(self, command: str) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(self._bash_sync, command)

    async def next_event(self, *, timeout: float | None = None) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(self._next_event_sync, timeout=timeout)

    async def wait_for_event(self, event_type: str, *, timeout: float | None = None) -> JsonObject:
        import asyncio
        return await asyncio.to_thread(self._wait_for_event_sync, event_type, timeout=timeout)

    async def get_last_assistant_text(self) -> str | None:
        import asyncio
        return await asyncio.to_thread(self._get_last_assistant_text_sync)

    # --- reader threads ---------------------------------------------------

    def _next_request_id(self) -> str:
        self._request_counter += 1
        return f"py-{self._request_counter}-{uuid.uuid4().hex}"

    def _read_stdout_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        stdout_stream = self.process.stdout  # F9 review fix: cache reference
        decoder = StrictJsonlDecoder()
        try:
            while True:
                chunk = stdout_stream.read(4096)
                if not chunk:
                    break
                for value in decoder.feed(chunk):
                    if isinstance(value, dict):
                        try:
                            self._handle_message(value)
                        except Exception:
                            logger.exception("stdout reader: _handle_message raised")
        except Exception as exc:
            if not self._closing.is_set():
                self._set_fatal(exc)
        finally:
            if not self._closing.is_set():
                self._set_fatal(
                    PiRpcProcessError(f"Pi stdout closed. stderr={self.stderr!r}"),
                )

    def _read_stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        stderr_stream = self.process.stderr  # F9 review fix: cache reference
        while True:
            try:
                chunk = stderr_stream.read(4096)
            except Exception as exc:
                # F13 review fix: don't silently exit; log at debug so a
                # broken stderr pipe is at least diagnosable.
                logger.debug("stderr reader exiting: %s", exc)
                return
            if not chunk:
                return
            self._stderr_chunks.append(chunk.decode("utf-8", errors="replace"))

    def _handle_message(self, message: JsonObject) -> None:
        msg_type = message.get("type")
        if msg_type == "response":
            req_id = message.get("id")
            if isinstance(req_id, str):
                fut = self._pop_pending(req_id)
                if fut is not None:
                    self._complete_future(fut, value=message)
            return

        if msg_type == "extension_ui_request":
            self._handle_extension_ui_request(message)
            return

        # Plain event delivery: queue first (dual-delivery ordering per
        # v4-synthesis), then dispatch to handlers.
        self._enqueue_event(message)
        self._dispatch_event(message)

    def _enqueue_event(self, event: JsonObject) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full:
            with self._events_drop_lock:
                # Drop oldest, retry put.
                with contextlib.suppress(queue.Empty):
                    self._events.get_nowait()
                with contextlib.suppress(queue.Full):
                    self._events.put_nowait(event)
                self._dropped_event_count += 1
                now = _monotonic()
                if now - self._last_drop_warning_time > 5.0:
                    logger.warning(
                        "rpc client dropped events (total=%d); consumer is too slow",
                        self._dropped_event_count,
                    )
                    self._last_drop_warning_time = now

    def _dispatch_event(self, event: JsonObject) -> None:
        if self._closing.is_set():
            return
        self._in_dispatch.value = True
        try:
            with self._handlers_lock:
                handlers = list(self._event_handlers)
            for handler in handlers:
                try:
                    result = handler(event)
                    self._check_handler_return(result, handler)
                    # F12 review fix: event handler returns are warned per
                    # v4 plan; UI handlers handle non-None at the call site.
                    if result is not None:
                        logger.warning(
                            "event handler %r returned non-None %r; return is ignored",
                            handler, type(result).__name__,
                        )
                except Exception:
                    logger.exception("event handler %r raised", handler)
        finally:
            self._in_dispatch.value = False

    def _handle_extension_ui_request(self, message: JsonObject) -> None:
        method = str(message.get("method") or "")
        req_id = message.get("id")
        if not isinstance(req_id, str):
            return

        is_notification = method in _NOTIFICATION_UI_METHODS

        # F2 review fix: set the UI-gate IMMEDIATELY for response-bearing
        # methods, BEFORE dispatching events. The previous order let
        # caller-thread sends interleave during the dual-delivery event
        # dispatch (a slow event handler created a multi-second window in
        # which the gate was clear). Notification-only methods bypass the
        # gate (no response is expected).
        if not is_notification:
            self._ui_response_pending.set()

        # Dual-delivery: queue + event handlers. Now safe — gate is up for
        # response-bearing methods so concurrent caller-thread sends back off.
        self._enqueue_event(message)
        self._dispatch_event(message)
        if is_notification:
            return
        response: JsonObject | None = None
        try:
            with self._handlers_lock:
                handler = self._ui_handlers.get(method) or self._fallback_ui_handler
            if handler is not None:
                self._in_dispatch.value = True
                try:
                    raw_response: Any = handler(message)
                    self._check_handler_return(raw_response, handler)
                    if raw_response is None:
                        response = None
                    elif isinstance(raw_response, collections.abc.Mapping):
                        response = dict(raw_response)
                    else:
                        logger.error(
                            "UI handler %r returned non-mapping %r; using default response",
                            handler, type(raw_response).__name__,
                        )
                        response = None
                except Exception:
                    logger.exception("UI handler %r raised; sending default response", handler)
                    response = None
            if response is None:
                response = self._default_ui_response(method)
            # F1 review fix: spread response FIRST so user-handler keys
            # cannot override the framework's required ``type``/``id``.
            # Otherwise a handler returning ``{"id": "evil"}`` silently
            # corrupts the response id (verified by Opus reviewer).
            self._write_stdin_ui_response(
                {**response, "type": "extension_ui_response", "id": req_id},
            )
        finally:
            self._in_dispatch.value = False  # F4 review fix: prevent leak
            self._ui_response_pending.clear()
            with self._ui_response_condition:
                self._ui_response_condition.notify_all()

    def _write_stdin_ui_response(self, frame: JsonObject) -> None:
        """Reader-owned UI response write. Bypasses the UI gate (we set it)."""
        if self._closing.is_set():
            return
        with self._send_lock:
            if self._stdin_closed:
                return
            proc = self.process
            if proc is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(dumps_line(frame))
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                # Best-effort: pi is likely gone.
                logger.debug("UI response write failed: %s", exc)

    def _check_handler_return(self, result: Any, handler: Callable[..., Any]) -> None:
        # Reject awaitable / async-gen / generator / AsyncIterable.
        if (
            inspect.isawaitable(result)
            or inspect.isasyncgen(result)
            or inspect.isgenerator(result)
            or isinstance(result, collections.abc.AsyncIterable)
        ):
            raise TypeError(
                f"event/UI handler {handler!r} returned awaitable/async-gen/gen/async-iter; "
                "see docs/DESIGN.md § Breaking changes",
            )
        # Non-None event handler returns are warned (not raised). UI handler
        # returns are handled by the caller (mapping vs not).

    def _default_ui_response(self, method: str) -> JsonObject:
        if method == "confirm":
            return {"confirmed": False}
        if method in {"select", "input", "editor"}:
            return {"cancelled": True}
        return {"cancelled": True}


# --- tiny helpers to keep test-friendliness -------------------------------


def _monotonic() -> float:
    """Indirection over ``time.monotonic`` for easy patching in tests."""
    import time

    return time.monotonic()


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
