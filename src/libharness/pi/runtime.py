"""Threaded runtime primitives for Python-owned Pi harnesses.

The default runtime shape is deliberately split:

* one dedicated asyncio loop thread owns async subprocess and socket I/O;
* one shared thread pool executes Python tools for every harness;
* each :class:`PiAgentHarness` has its own owner thread for harness state.

The application main thread can therefore remain an application thread rather
than being consumed by the library's event loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
from collections.abc import Coroutine
from typing import Any


class AsyncioLoopThread:
    """A reusable asyncio event loop running on a dedicated background thread."""

    def __init__(self, *, thread_name: str = "PiAsyncioLoop-Thread-2", daemon: bool = True) -> None:
        self.thread_name = thread_name
        self.daemon = daemon
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._start_lock = threading.Lock()
        self._closed = False
        self._thread_id: int | None = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        self.start()
        assert self._loop is not None
        return self._loop

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    @property
    def thread_id(self) -> int | None:
        return self._thread_id

    @property
    def is_running(self) -> bool:
        loop = self._loop
        return bool(loop and loop.is_running() and self._thread and self._thread.is_alive())

    def start(self) -> AsyncioLoopThread:
        with self._start_lock:
            if self.is_running:
                return self
            if self._closed:
                raise RuntimeError("AsyncioLoopThread has been closed")
            self._ready.clear()
            self._stopped.clear()
            self._thread = threading.Thread(
                target=self._run, name=self.thread_name, daemon=self.daemon
            )
            self._thread.start()
            # Keep `_ready.wait()` inside the lock. Otherwise two threads can
            # both pass the `is_running` check (the first thread hasn't set
            # the loop running yet), each construct and start a Thread, and
            # the second stomp `self._thread` — leaking the first and routing
            # all subsequent work onto a thread `stop()` won't join.
            self._ready.wait()
            if self._loop is None:
                raise RuntimeError("asyncio loop thread failed to start")
            return self

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        """Schedule *coro* on the loop thread and return a concurrent future."""

        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro: Coroutine[Any, Any, Any], *, timeout: float | None = None) -> Any:
        """Schedule *coro* on the loop thread and block for its result."""

        return self.submit(coro).result(timeout=timeout)

    def call_soon(self, callback: Any, *args: Any) -> None:
        self.loop.call_soon_threadsafe(callback, *args)

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._start_lock:
            loop = self._loop
            thread = self._thread
            if loop is None or thread is None:
                self._closed = True
                return
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            if thread.is_alive():
                thread.join(timeout=timeout)
            self._closed = True

    close = stop

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._thread_id = threading.get_ident()
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            with contextlib.suppress(AttributeError):  # pragma: no cover - Python < 3.9 compatibility path
                loop.run_until_complete(loop.shutdown_default_executor())
            asyncio.set_event_loop(None)
            loop.close()
            self._stopped.set()


class HarnessRuntime:
    """Shared runtime resources for one or more :class:`PiAgentHarness` objects."""

    def __init__(
        self,
        *,
        loop_thread: AsyncioLoopThread | None = None,
        tool_executor: concurrent.futures.ThreadPoolExecutor | None = None,
        hook_executor: concurrent.futures.ThreadPoolExecutor | None = None,
        max_tool_workers: int | None = None,
        loop_thread_name: str = "PiAsyncioLoop-Thread-2",
        tool_thread_name_prefix: str = "pi-tool",
        hook_thread_name_prefix: str = "pi-hook",
    ) -> None:
        self.loop_thread = loop_thread or AsyncioLoopThread(thread_name=loop_thread_name)
        self.tool_executor = tool_executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=max_tool_workers,
            thread_name_prefix=tool_thread_name_prefix,
        )
        self.hook_executor = hook_executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=hook_thread_name_prefix,
        )
        self._owns_loop_thread = loop_thread is None
        self._owns_tool_executor = tool_executor is None
        self._owns_hook_executor = hook_executor is None
        self._closed = False

    @property
    def loop_thread_id(self) -> int | None:
        return self.loop_thread.thread_id

    @property
    def loop_thread_name(self) -> str:
        thread = self.loop_thread.thread
        return thread.name if thread is not None else self.loop_thread.thread_name

    def start(self) -> HarnessRuntime:
        if self._closed:
            raise RuntimeError("HarnessRuntime has been closed")
        self.loop_thread.start()
        return self

    def submit_async(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future[Any]:
        self.start()
        return self.loop_thread.submit(coro)

    def run_async(self, coro: Coroutine[Any, Any, Any], *, timeout: float | None = None) -> Any:
        self.start()
        return self.loop_thread.run(coro, timeout=timeout)

    def close(
        self, *, shutdown_executor: bool = True, cancel_futures: bool = True, timeout: float = 5.0
    ) -> None:
        if self._closed:
            return
        if self._owns_loop_thread:
            self.loop_thread.stop(timeout=timeout)
        if shutdown_executor and self._owns_tool_executor:
            self.tool_executor.shutdown(wait=True, cancel_futures=cancel_futures)
        if shutdown_executor and self._owns_hook_executor:
            self.hook_executor.shutdown(wait=True, cancel_futures=cancel_futures)
        self._closed = True

    def __enter__(self) -> HarnessRuntime:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


_default_runtime: HarnessRuntime | None = None
_default_runtime_lock = threading.Lock()


def get_default_runtime() -> HarnessRuntime:
    """Return the process-wide runtime shared by default harnesses."""

    global _default_runtime
    with _default_runtime_lock:
        if _default_runtime is None or _default_runtime._closed:
            _default_runtime = HarnessRuntime()
        return _default_runtime


def close_default_runtime() -> None:
    """Close the process-wide default runtime, primarily for tests."""

    global _default_runtime
    with _default_runtime_lock:
        runtime = _default_runtime
        _default_runtime = None
    if runtime is not None:
        runtime.close()
