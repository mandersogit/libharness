"""Thread-owned Pi agent harness facade.

``PiAgentHarness`` is the threaded shape requested for application embedding:

* the application main thread keeps control of the application;
* one shared asyncio loop thread owns asynchronous Pi RPC and bridge I/O;
* each harness owns its mutable harness state on its own owner thread;
* Python tools for all harnesses run in one shared thread pool.

For tests, pass ``threaded=False`` and the harness core lives on the current
thread, including ``MainThread``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import queue
import tempfile
import threading
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar, cast, overload

from .hook_surface import validate_decision_timeouts_mapping
from .rpc import PiLaunchConfig, PiRpcClient
from .runtime import HarnessRuntime, get_default_runtime
from .server import BridgeEndpoint, PythonToolServer
from .shim import write_bridge_shim, write_faux_toolcall_provider_extension
from .tools import ToolRegistry


@dataclass(frozen=True, slots=True)
class HarnessSnapshot:
    """Threading and lifecycle facts for a harness instance."""

    harness_id: str
    started: bool
    owner_thread_id: int
    owner_thread_name: str
    async_loop_thread_id: int | None
    async_loop_thread_name: str
    registry_frozen: bool
    bridge_endpoint: BridgeEndpoint | None
    extension_paths: tuple[Path, ...]


_LOG = logging.getLogger(__name__)

_T = TypeVar("_T")
_Command = tuple[Callable[["_PiAgentHarnessCore"], Any], concurrent.futures.Future[Any]]
_STOP = object()
_WAKE = object()
"""Sentinel that ``_pump_until`` enqueues on its waited-future's
done-callback. Wakes the queue consumer out of ``queue.get()`` so it can
re-check the future. Recognized as a no-op by any consumer
(``_pump_until`` itself OR the owner-thread loop) so stale wakes from
prior pumps don't break anything."""


@dataclass(frozen=True, slots=True)
class _Continuation:
    """Owner-thread message: a loop-future completed, propagate to main_future.

    Operations submitted via ``submit()`` may return a ``concurrent.futures.Future``
    (indicating the operation hands off async work to the loop). The owner
    thread registers a done callback on that future which posts a
    ``_Continuation`` back to its queue. When processed, the continuation
    propagates the loop's result to the caller's main_future.

    F8: this is the mechanism that keeps the owner thread free to handle
    sync hook dispatch while a long-running RPC is in flight on the loop.
    """

    loop_future: concurrent.futures.Future[Any]
    main_future: concurrent.futures.Future[Any]


@dataclass(frozen=True, slots=True)
class _HookCall:
    """Owner-thread message: invoke a sync hook from the loop.

    F8: sync hooks (``on_<event>``, ``decide_<event>``) cannot run on the
    asyncio loop thread (cooperative scheduling), and shouldn't run on the
    tool pool (saturation risk). They run on the owner thread instead —
    naturally serialized with the harness's RPC dispatch.

    For notification hooks, ``completion`` is set so the loop can await
    completion (preserves FIFO event ordering). For decision hooks,
    ``completion`` is also set and additionally carries the hook's return
    value (which the loop hands back to pi).
    """

    fn: Callable[[], Any]
    event_name: str
    completion: concurrent.futures.Future[Any] | None
_BridgeEventHandler = Callable[
    [Mapping[str, Any], threading.Event, bool, str | None], Awaitable[object | None]
]


class PiAgentHarness:
    """Synchronous, thread-owned facade around Pi RPC and Python tools.

    Normal application mode uses ``threaded=True``. The Python object you hold is
    a proxy; the harness core, including lifecycle state and Pi client reference,
    is created on an owner thread. Public methods marshal work onto that owner
    thread and block until the result is available.

    Test mode can use ``threaded=False``. In that mode the core is created on the
    current thread and public methods execute directly. This makes unit tests
    simple and permits one harness to live on ``MainThread``.
    """

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        config: PiLaunchConfig | None = None,
        runtime: HarnessRuntime | None = None,
        threaded: bool = True,
        owner_thread_name: str | None = None,
        workdir: str | Path | None = None,
        shim_path: str | Path | None = None,
        diagnostic_commands: bool = True,
        keep_temp: bool = False,
        fake_provider: bool = False,
        fake_tool_name: str | None = None,
        fake_tool_args: dict[str, Any] | None = None,
        fake_provider_name: str = "pyharness-test",
        fake_model_id: str = "pyharness-faux-1",
        fake_final_text: str = "done",
        bridge_event_handler: _BridgeEventHandler | None = None,
        initial_open_gates: Sequence[str] = (),
        decision_timeouts_ms: Mapping[str, int] | None = None,
        start_owner_thread: bool = True,
    ) -> None:
        # F25: validate the constructor-supplied decision_timeouts_ms before
        # storing it. Agent subclasses skip this path (their __init__ raises
        # if the kwarg is passed; the classmethod path validates against
        # _DECISION_EVENT_NAMES). Direct PiAgentHarness callers can pass it,
        # so check basic types here.
        if decision_timeouts_ms is not None:
            validate_decision_timeouts_mapping(decision_timeouts_ms)
        self.harness_id = uuid.uuid4().hex[:12]
        self.registry = registry or ToolRegistry()
        self.config = config or PiLaunchConfig()
        self.runtime = runtime or get_default_runtime()
        self.threaded = threaded
        self._owns_runtime = runtime is None
        self._closed = False
        # F8 / Level-3-Option-A: queue exists in both modes. In threaded=True
        # the dedicated owner thread consumes it; in threaded=False MainThread
        # pumps it via `_call`/`pump_until`. Same dispatcher, different consumer.
        self._commands: queue.Queue[Any] = queue.Queue()
        self._owner_thread: threading.Thread | None = None
        self._owner_thread_name = owner_thread_name
        self._owner_ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._owner_start_lock = threading.Lock()
        # F9: guards the close↔submit race. Without it, submit()'s
        # `if self._closed` check and the subsequent `_commands.put(command)`
        # can interleave with close()'s `self._closed = True` +
        # `_commands.put(_STOP)`, leaving the late command queued behind
        # _STOP and hanging the caller forever. Holding this lock around
        # both the check-and-put in submit() and the set-and-put in
        # close() keeps the operation atomic.
        self._close_lock = threading.Lock()
        self._core: _PiAgentHarnessCore | None = None
        self._core_kwargs: dict[str, Any] = {
            "harness_id": self.harness_id,
            "registry": self.registry,
            "config": self.config,
            "runtime": self.runtime,
            "workdir": Path(workdir) if workdir is not None else None,
            "shim_path": Path(shim_path) if shim_path is not None else None,
            "diagnostic_commands": diagnostic_commands,
            "keep_temp": keep_temp,
            "fake_provider": fake_provider,
            "fake_tool_name": fake_tool_name,
            "fake_tool_args": fake_tool_args or {},
            "fake_provider_name": fake_provider_name,
            "fake_model_id": fake_model_id,
            "fake_final_text": fake_final_text,
            "bridge_event_handler": bridge_event_handler,
            "initial_open_gates": tuple(initial_open_gates),
            "decision_timeouts_ms": dict(decision_timeouts_ms or {}),
        }

        if threaded and start_owner_thread:
            self.start_owner_thread()
        elif not threaded:
            self._core = _PiAgentHarnessCore(**self._core_kwargs)
            self._owner_ready.set_result(None)

    @property
    def owner_thread(self) -> threading.Thread | None:
        return self._owner_thread

    @property
    def owner_thread_id(self) -> int | None:
        if self._core is not None:
            return self._core.owner_thread_id
        return self._owner_thread.ident if self._owner_thread else None

    @property
    def owner_thread_name(self) -> str | None:
        if self._core is not None:
            return self._core.owner_thread_name
        return self._owner_thread.name if self._owner_thread else None

    def start_owner_thread(self) -> PiAgentHarness:
        if not self.threaded:
            return self
        # Guard the check + assign + start + wait sequence under a lock.
        # Two concurrent callers can both pass the `is None` check, both
        # construct a Thread, and the second stomp `self._owner_thread` —
        # the first thread then trips `_owner_ready.set_result(None)` and
        # the second hits InvalidStateError. The lock keeps "exactly one
        # owner thread per harness" true regardless of caller threading.
        with self._owner_start_lock:
            if self._owner_thread is not None:
                return self
            # Start the shared loop first. In normal embedding this makes the
            # asyncio loop the first library-owned thread, leaving later threads
            # for individual harness owners.
            self.runtime.start()
            name = self._owner_thread_name or f"PiAgentHarness-{self.harness_id}"
            self._owner_thread = threading.Thread(
                target=self._owner_loop, name=name, daemon=True
            )
            self._owner_thread.start()
            self._owner_ready.result(timeout=10)
            return self

    def start(self) -> PiAgentHarness:
        self._call(lambda core: core.start())
        return self

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._call(lambda core: core.close())
        finally:
            # F9: take the close lock so an interleaving submit() can't
            # land a command between us setting _closed and putting _STOP.
            with self._close_lock:
                self._closed = True
                if self.threaded:
                    assert self._commands is not None
                    self._commands.put(_STOP)
            if (
                self.threaded
                and self._owner_thread is not None
                and threading.get_ident() != self._owner_thread.ident
            ):
                self._owner_thread.join(timeout=5)

    def snapshot(self) -> HarnessSnapshot:
        return self._call(lambda core: core.snapshot())

    def send(self, command: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        wait_timeout = None if timeout is None else timeout + 1.0
        return self._call(
            lambda core: core.call_rpc("send", command, wait_timeout=wait_timeout, timeout=timeout)
        )

    def prompt(
        self, message: str, *, timeout: float | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self._call(
            lambda core: core.call_rpc("prompt", message, wait_timeout=timeout, **kwargs)
        )

    def prompt_and_wait(
        self, message: str, *, timeout: float = 120.0, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return self._call(
            lambda core: core.call_rpc(
                "prompt_and_wait", message, wait_timeout=timeout + 1.0, timeout=timeout, **kwargs
            )
        )

    def steer(self, message: str, *, timeout: float | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._call(
            lambda core: core.call_rpc("steer", message, wait_timeout=timeout, **kwargs)
        )

    def follow_up(
        self, message: str, *, timeout: float | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        return self._call(
            lambda core: core.call_rpc("follow_up", message, wait_timeout=timeout, **kwargs)
        )

    def abort(self, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call(lambda core: core.call_rpc("abort", wait_timeout=timeout))

    def get_state(self, *, timeout: float | None = None) -> dict[str, Any]:
        return self._call(lambda core: core.call_rpc("get_state", wait_timeout=timeout))

    def get_messages(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        return self._call(lambda core: core.call_rpc("get_messages", wait_timeout=timeout))

    def get_commands(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        return self._call(lambda core: core.call_rpc("get_commands", wait_timeout=timeout))

    def get_available_models(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        return self._call(lambda core: core.call_rpc("get_available_models", wait_timeout=timeout))

    def set_model(
        self, provider: str, model_id: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        return self._call(
            lambda core: core.call_rpc("set_model", provider, model_id, wait_timeout=timeout)
        )

    def submit(
        self, operation: Callable[[_PiAgentHarnessCore], _T]
    ) -> concurrent.futures.Future[_T]:
        """Submit a custom owner-thread operation and return a future.

        This is intentionally lower-level than the convenience methods. It is
        useful for application code that wants non-blocking interaction with a
        harness while preserving owner-thread affinity.
        """

        if not self.threaded:
            # Fast path — no lock needed because there's no queue to race
            # against. The closed check is still done here for the same
            # error message.
            if self._closed:
                raise RuntimeError("PiAgentHarness is closed")
            future: concurrent.futures.Future[_T] = concurrent.futures.Future()
            try:
                future.set_result(self._call(operation))
            except BaseException as exc:
                future.set_exception(exc)
            return future
        # C2: detect re-entry from a sync hook running on the owner thread.
        # The owner thread is the queue's only consumer; if it's currently
        # mid-hook and the hook calls a public API method that goes through
        # `submit`, the queued operation would wait for the owner thread to
        # finish the hook — which is waiting for the operation. Deadlock.
        # Raise loudly with an actionable message instead.
        owner_thread = self._owner_thread
        if owner_thread is not None and threading.get_ident() == owner_thread.ident:
            raise RuntimeError(
                "PiAgentHarness public API called from the harness's own owner "
                "thread (usually a sync `on_*` / `decide_*` hook). This would "
                "deadlock the owner-thread queue. Use an `async_*` hook instead, "
                "or perform the API call from a separate thread."
            )
        # F9: ensure start_owner_thread runs before we take the close lock —
        # start_owner_thread takes its own lock and we don't want to nest.
        self.start_owner_thread()
        future = concurrent.futures.Future()
        assert self._commands is not None
        command = (
            cast(Callable[["_PiAgentHarnessCore"], Any], operation),
            cast(concurrent.futures.Future[Any], future),
        )
        # F9: hold _close_lock for the check-and-put so a concurrent close()
        # can't slip _STOP between the check and the put. Either we put
        # before close (the command runs normally) or we see _closed=True
        # and raise — never queued-behind-STOP.
        with self._close_lock:
            if self._closed:
                raise RuntimeError("PiAgentHarness is closed")
            self._commands.put(command)
        return future

    def __enter__(self) -> PiAgentHarness:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @overload
    def _call(
        self,
        operation: Callable[[_PiAgentHarnessCore], concurrent.futures.Future[_T]],
    ) -> _T: ...
    @overload
    def _call(self, operation: Callable[[_PiAgentHarnessCore], _T]) -> _T: ...
    def _call(self, operation: Callable[[_PiAgentHarnessCore], Any]) -> Any:
        """Run an operation on the harness's owner thread; block for the result.

        Operations may return a value (sync) or a ``concurrent.futures.Future``
        (the operation handed work off to the asyncio loop; F8). The
        dispatcher unwraps either case so callers see the value.

        Level-3-Option-A: in ``threaded=False`` mode, MainThread *is* the
        owner thread, so we pump the command queue here while waiting for
        our operation's result. This is the same dispatcher loop the owner
        thread runs in ``threaded=True`` mode — just consumed by MainThread
        instead. Single code path; consumer varies with mode.
        """
        if self._closed:
            raise RuntimeError("PiAgentHarness is closed")
        if self.threaded:
            return self.submit(operation).result()
        # threaded=False: MainThread pumps the queue.
        if self._core is None:
            self._core = _PiAgentHarnessCore(**self._core_kwargs)
        main_future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._commands.put((operation, main_future))
        self._pump_until(main_future)
        return main_future.result()

    def pump_until(
        self,
        target: Awaitable[Any] | concurrent.futures.Future[Any] | threading.Event,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Submit a coro / wait on a Future / wait on an Event; pump the
        harness command queue while waiting.

        For ``threaded=False`` callers (tests) that inject events directly
        bypassing the harness public API — e.g. ``agent.pump_until(
        agent._async_on_event({"type": "agent_start"}))``. While MainThread
        pumps, sync hooks dispatched from the loop are picked up and run
        on MainThread, preserving the single-code-path invariant for sync
        hook dispatch (Level-3-Option-A).

        In ``threaded=True`` mode the owner thread is already pumping;
        ``pump_until`` simply blocks on the target.

        Target types:

        - ``Awaitable`` / ``Coroutine`` — submitted to the loop; pump until
          the resulting loop-future resolves; return the resolved value.
        - ``concurrent.futures.Future`` — wait on it; return resolved value.
        - ``threading.Event`` — wait until set (poll-based; ignores
          completion futures since events don't have done-callbacks).
          Returns the event's ``is_set()`` state. Honors ``timeout``.
        """
        if isinstance(target, threading.Event):
            if self.threaded:
                return target.wait(timeout)
            if self._core is None:
                self._core = _PiAgentHarnessCore(**self._core_kwargs)
            return self._pump_until_event(target, timeout=timeout)
        if isinstance(target, concurrent.futures.Future):
            loop_future = target
        else:
            loop_future = self.runtime.submit_async(
                cast("Coroutine[Any, Any, Any]", target)
            )
        if self.threaded:
            return loop_future.result(timeout=timeout)
        if self._core is None:
            self._core = _PiAgentHarnessCore(**self._core_kwargs)
        self._pump_until(loop_future)
        return loop_future.result()

    def _pump_until_event(self, event: threading.Event, *, timeout: float | None) -> bool:
        """Pump the queue until ``event`` is set or the timeout expires.

        Polls the event with a short queue-get timeout (events don't
        provide done-callbacks the way Futures do, so the wake-sentinel
        trick doesn't apply).
        """
        import time as _time

        deadline = _time.monotonic() + timeout if timeout is not None else None
        while not event.is_set():
            if deadline is not None and _time.monotonic() >= deadline:
                return False
            try:
                item = self._commands.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is _WAKE:
                continue
            if item is _STOP:
                self._commands.put(_STOP)
                raise RuntimeError("PiAgentHarness is closing")
            self._process_item(item)
        return True

    def _pump_until(self, future: concurrent.futures.Future[Any]) -> None:
        """Drain the command queue until ``future`` is done.

        Used by both ``_call`` (waits for the operation's main_future) and
        ``pump_until`` (waits for an arbitrary loop future). MainThread
        becomes the queue consumer for the duration.

        We register a done-callback on ``future`` that pushes a wake
        sentinel onto the queue when the future resolves. Without this,
        a future that completes without enqueueing any further work
        (e.g. ``pump_until`` waiting on a loop coro that doesn't fire
        any sync hooks) would leave ``queue.get()`` blocked forever.
        """
        future.add_done_callback(lambda _: self._commands.put(_WAKE))
        while not future.done():
            item = self._commands.get()
            if item is _WAKE:
                continue
            if item is _STOP:
                # If a close races in, re-queue it and bail (the close-lock
                # guarantees _STOP is the last thing on the queue).
                self._commands.put(_STOP)
                raise RuntimeError("PiAgentHarness is closing")
            self._process_item(item)

    def _process_item(self, item: Any) -> None:
        """Dispatch one command-queue item. Shared by owner thread + pump."""
        if item is _WAKE:
            # Stale wake sentinel from a `_pump_until` that already exited.
            # No-op; the original waiter is gone.
            return
        if isinstance(item, _Continuation):
            self._handle_continuation(item)
            return
        if isinstance(item, _HookCall):
            self._handle_hook_call(item)
            return
        # Legacy (operation, main_future) tuple — operation may return a
        # value (sync) or a concurrent.futures.Future (async; loop-bound).
        operation, future = cast(_Command, item)
        if not future.set_running_or_notify_cancel():
            return
        assert self._core is not None, "core must be constructed before dispatch"
        try:
            result = operation(self._core)
        except BaseException as exc:
            future.set_exception(exc)
            return
        if isinstance(result, concurrent.futures.Future):
            self._register_continuation(result, future)
        else:
            future.set_result(result)

    def _owner_loop(self) -> None:
        try:
            self._core = _PiAgentHarnessCore(**self._core_kwargs)
            self._owner_ready.set_result(None)
            while True:
                item = self._commands.get()
                if item is _STOP:
                    return
                self._process_item(item)
        except BaseException as exc:
            if not self._owner_ready.done():
                self._owner_ready.set_exception(exc)
            raise

    def _register_continuation(
        self,
        loop_future: concurrent.futures.Future[Any],
        main_future: concurrent.futures.Future[Any],
    ) -> None:
        """Wire ``loop_future`` to deliver back to the owner queue.

        When ``loop_future`` completes (on the asyncio loop thread), its
        done-callback enqueues a ``_Continuation`` on this harness's
        command queue. The owner thread will then process the continuation,
        propagating the result to ``main_future``.

        C1: close-coordinate the enqueue. If ``close()`` already set
        ``_closed`` and put ``_STOP``, enqueueing a ``_Continuation``
        behind ``_STOP`` would strand the caller's ``main_future``
        forever (owner thread exits on ``_STOP`` without draining).
        Take ``_close_lock``; if closed, complete ``main_future`` with
        an exception so the awaiting ``_call`` / ``submit().result()``
        unblocks immediately.
        """
        commands = self._commands
        close_lock = self._close_lock
        closed_flag = self  # captured for closure; read .self._closed lazily

        def _enqueue(lf: concurrent.futures.Future[Any]) -> None:
            with close_lock:
                if closed_flag._closed:
                    with contextlib.suppress(concurrent.futures.InvalidStateError):
                        main_future.set_exception(
                            RuntimeError(
                                "PiAgentHarness is closed; "
                                "continuation cannot be delivered"
                            )
                        )
                    return
                try:
                    commands.put(_Continuation(lf, main_future))
                except Exception:  # pragma: no cover — queue is unbounded
                    _LOG.exception("failed to enqueue _Continuation")
                    with contextlib.suppress(concurrent.futures.InvalidStateError):
                        main_future.set_exception(
                            RuntimeError("failed to enqueue _Continuation")
                        )

        loop_future.add_done_callback(_enqueue)

    @staticmethod
    def _handle_continuation(item: _Continuation) -> None:
        main = item.main_future
        if main.cancelled() or main.done():
            return
        try:
            main.set_result(item.loop_future.result())
        except BaseException as exc:
            with contextlib.suppress(concurrent.futures.InvalidStateError):
                main.set_exception(exc)

    @staticmethod
    def _handle_hook_call(item: _HookCall) -> None:
        try:
            value = item.fn()
        except BaseException as exc:
            if item.completion is not None and not item.completion.done():
                item.completion.set_exception(exc)
            else:
                _LOG.exception("Agent sync hook failed for event %s", item.event_name)
            return
        if item.completion is not None and not item.completion.done():
            item.completion.set_result(value)


class _PiAgentHarnessCore:
    """Owner-thread-only mutable harness core."""

    def __init__(
        self,
        *,
        harness_id: str,
        registry: ToolRegistry,
        config: PiLaunchConfig,
        runtime: HarnessRuntime,
        workdir: Path | None,
        shim_path: Path | None,
        diagnostic_commands: bool,
        keep_temp: bool,
        fake_provider: bool,
        fake_tool_name: str | None,
        fake_tool_args: dict[str, Any],
        fake_provider_name: str,
        fake_model_id: str,
        fake_final_text: str,
        bridge_event_handler: _BridgeEventHandler | None,
        initial_open_gates: Sequence[str],
        decision_timeouts_ms: Mapping[str, int],
    ) -> None:
        self.harness_id = harness_id
        self.registry = registry
        self.config = config
        self.runtime = runtime
        self.workdir = workdir
        self.shim_path = shim_path
        self.diagnostic_commands = diagnostic_commands
        self.keep_temp = keep_temp
        self.fake_provider = fake_provider
        self.fake_tool_name = fake_tool_name
        self.fake_tool_args = fake_tool_args
        self.fake_provider_name = fake_provider_name
        self.fake_model_id = fake_model_id
        self.fake_final_text = fake_final_text
        self.bridge_event_handler = bridge_event_handler
        self.initial_open_gates = tuple(initial_open_gates)
        self.decision_timeouts_ms = dict(decision_timeouts_ms)
        self.server: PythonToolServer | None = None
        self.pi: PiRpcClient | None = None
        self.endpoint: BridgeEndpoint | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._extension_paths: list[Path] = []
        self.owner_thread_id = threading.get_ident()
        self.owner_thread_name = threading.current_thread().name

    def start(self) -> concurrent.futures.Future[None]:
        """Begin starting pi + the bridge; return the loop's completion future.

        F8: the synchronous owner-thread work (constructing objects, writing
        shim files) happens here; the I/O-bound async work is handed off to
        the loop via ``runtime.submit_async``. The owner thread is then free
        to handle hook dispatches that may fire as soon as pi launches.
        """
        self._check_owner()
        if self.pi is not None:
            raise RuntimeError("PiAgentHarness is already started")

        self.runtime.start()
        self.registry.freeze()
        self.server = PythonToolServer(
            self.registry,
            tool_executor=self.runtime.tool_executor,
            event_handler=self.bridge_event_handler,
            initial_open_gates=self.initial_open_gates,
            decision_timeouts_ms=self.decision_timeouts_ms,
        )

        root = self._artifact_root()
        shim_path = self.shim_path or root / "python_tools_extension.ts"
        write_bridge_shim(shim_path, diagnostic_commands=self.diagnostic_commands)
        extension_paths = [shim_path]

        provider = self.config.provider
        model = self.config.model
        env = dict(self.config.env or {})
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        env.setdefault("PI_OFFLINE", "1")
        env.setdefault("PI_PY_DIAGNOSTIC_COMMANDS", "1" if self.diagnostic_commands else "0")
        env.setdefault("PI_PY_HARNESS_ID", self.harness_id)

        if self.fake_provider:
            tool_name = self.fake_tool_name or _first_tool_name(self.registry)
            faux_path = root / "faux_toolcall_provider.ts"
            write_faux_toolcall_provider_extension(
                faux_path,
                tool_name=tool_name,
                arguments=self.fake_tool_args,
                provider=self.fake_provider_name,
                model_id=self.fake_model_id,
                final_text=self.fake_final_text,
            )
            extension_paths.append(faux_path)
            provider = provider or self.fake_provider_name
            model = model or self.fake_model_id
            env.setdefault("PYHARNESS_FAUX_API_KEY", "test")

        self._extension_paths = extension_paths
        startup_timeout = max(10.0, self.config.startup_timeout + 5.0)
        server = self.server

        async def _start_io() -> None:
            assert server is not None
            endpoint = await server.start()
            # F8: self.endpoint is read by snapshot() from the owner thread.
            # During start, no concurrent reads happen (no caller has
            # observed the harness as started yet). Single write, then the
            # awaited pi.start completes before the public start() future
            # resolves — at which point snapshot() callers see the
            # fully-initialized state.
            self.endpoint = endpoint
            env_with_endpoint = dict(env)
            env_with_endpoint.update(endpoint.env())
            config = replace(
                self.config, env=env_with_endpoint, provider=provider, model=model
            )
            pi = PiRpcClient(config)
            self.pi = pi
            await asyncio.wait_for(
                pi.start(extension_paths=extension_paths), timeout=startup_timeout
            )

        return self.runtime.submit_async(_start_io())

    def close(self) -> concurrent.futures.Future[None]:
        """Shut down pi + the bridge; return the loop's completion future."""
        self._check_owner()
        pi = self.pi
        server = self.server
        tempdir = self._tempdir
        self.pi = None
        self.server = None
        self._tempdir = None
        self.endpoint = None
        self._extension_paths = []

        async def _close_io() -> None:
            if pi is not None:
                await asyncio.wait_for(pi.close(), timeout=10.0)
            if server is not None:
                await asyncio.wait_for(server.close(), timeout=10.0)
            if tempdir is not None and not self.keep_temp:
                tempdir.cleanup()

        return self.runtime.submit_async(_close_io())

    def call_rpc(
        self, method_name: str, *args: Any, wait_timeout: float | None = None, **kwargs: Any
    ) -> concurrent.futures.Future[Any]:
        """Submit an RPC to pi; return the loop's completion future.

        F8: returning a Future (not blocking on .result()) frees the owner
        thread to process sync hook dispatches that may fire during the RPC.
        """
        self._check_owner()
        if self.pi is None:
            raise RuntimeError("PiAgentHarness is not started")
        method = getattr(self.pi, method_name)
        inner: Awaitable[Any] = method(*args, **kwargs)

        async def _runner() -> Any:
            if wait_timeout is not None:
                return await asyncio.wait_for(inner, timeout=wait_timeout)
            return await inner

        return self.runtime.submit_async(_runner())

    def subscribe_client_events(
        self, handler: Callable[[dict[str, Any]], Any]
    ) -> concurrent.futures.Future[Callable[[], None]]:
        self._check_owner()
        if self.pi is None:
            raise RuntimeError("PiAgentHarness is not started")

        async def install() -> Callable[[], None]:
            assert self.pi is not None
            return self.pi.on_event(handler)

        async def _timed() -> Callable[[], None]:
            return await asyncio.wait_for(install(), timeout=5.0)

        return self.runtime.submit_async(_timed())

    def unsubscribe_client_events(
        self, unsubscribe: Callable[[], None]
    ) -> concurrent.futures.Future[None]:
        self._check_owner()

        async def uninstall() -> None:
            unsubscribe()

        async def _timed() -> None:
            await asyncio.wait_for(uninstall(), timeout=5.0)

        return self.runtime.submit_async(_timed())

    def snapshot(self) -> HarnessSnapshot:
        self._check_owner()
        return HarnessSnapshot(
            harness_id=self.harness_id,
            started=self.pi is not None,
            owner_thread_id=self.owner_thread_id,
            owner_thread_name=self.owner_thread_name,
            async_loop_thread_id=self.runtime.loop_thread_id,
            async_loop_thread_name=self.runtime.loop_thread_name,
            registry_frozen=self.registry.frozen,
            bridge_endpoint=self.endpoint,
            extension_paths=tuple(self._extension_paths),
        )

    @property
    def extension_paths(self) -> Sequence[Path]:
        self._check_owner()
        return tuple(self._extension_paths)

    def _artifact_root(self) -> Path:
        self._check_owner()
        if self.workdir is not None:
            self.workdir.mkdir(parents=True, exist_ok=True)
            return self.workdir
        if self._tempdir is None:
            self._tempdir = tempfile.TemporaryDirectory(
                prefix=f"pi-agent-harness-{self.harness_id}-"
            )
        return Path(self._tempdir.name)

    def _check_owner(self) -> None:
        if threading.get_ident() != self.owner_thread_id:
            raise RuntimeError(
                f"PiAgentHarness core (harness_id={self.harness_id!r}) is owned by "
                f"thread {self.owner_thread_name!r}; "
                f"current thread is {threading.current_thread().name!r}"
            )


def _first_tool_name(registry: ToolRegistry) -> str:
    for registered in registry:
        return registered.spec.name
    raise RuntimeError("fake_provider requires at least one registered Python tool")
