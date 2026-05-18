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

import concurrent.futures
import queue
import tempfile
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeVar, cast

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


_T = TypeVar("_T")
_Command = tuple[Callable[["_PiAgentHarnessCore"], Any], concurrent.futures.Future[Any]]
_STOP = object()
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
        self.harness_id = uuid.uuid4().hex[:12]
        self.registry = registry or ToolRegistry()
        self.config = config or PiLaunchConfig()
        self.runtime = runtime or get_default_runtime()
        self.threaded = threaded
        self._owns_runtime = runtime is None
        self._closed = False
        self._commands: queue.Queue[_Command | object] | None = queue.Queue() if threaded else None
        self._owner_thread: threading.Thread | None = None
        self._owner_thread_name = owner_thread_name
        self._owner_ready: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._owner_start_lock = threading.Lock()
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
            self._closed = True
            if self.threaded:
                assert self._commands is not None
                self._commands.put(_STOP)
                if (
                    self._owner_thread is not None
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

        if self._closed:
            raise RuntimeError("PiAgentHarness is closed")
        if not self.threaded:
            future: concurrent.futures.Future[_T] = concurrent.futures.Future()
            try:
                future.set_result(self._call(operation))
            except BaseException as exc:
                future.set_exception(exc)
            return future
        self.start_owner_thread()
        future = concurrent.futures.Future()
        assert self._commands is not None
        command = (
            cast(Callable[["_PiAgentHarnessCore"], Any], operation),
            cast(concurrent.futures.Future[Any], future),
        )
        self._commands.put(command)
        return future

    def __enter__(self) -> PiAgentHarness:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _call(self, operation: Callable[[_PiAgentHarnessCore], _T]) -> _T:
        if self._closed:
            raise RuntimeError("PiAgentHarness is closed")
        if not self.threaded:
            if self._core is None:
                self._core = _PiAgentHarnessCore(**self._core_kwargs)
            return operation(self._core)
        return self.submit(operation).result()

    def _owner_loop(self) -> None:
        try:
            self._core = _PiAgentHarnessCore(**self._core_kwargs)
            self._owner_ready.set_result(None)
            assert self._commands is not None
            while True:
                item = self._commands.get()
                if item is _STOP:
                    return
                operation, future = cast(_Command, item)
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(operation(self._core))
                    except BaseException as exc:
                        future.set_exception(exc)
        except BaseException as exc:
            if not self._owner_ready.done():
                self._owner_ready.set_exception(exc)
            raise


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

    def start(self) -> HarnessSnapshot:
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
        self.endpoint = cast(BridgeEndpoint, self.runtime.run_async(self.server.start()))

        root = self._artifact_root()
        shim_path = self.shim_path or root / "python_tools_extension.ts"
        write_bridge_shim(shim_path, diagnostic_commands=self.diagnostic_commands)
        extension_paths = [shim_path]

        provider = self.config.provider
        model = self.config.model
        env = dict(self.config.env or {})
        env.update(self.endpoint.env())
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

        config = replace(self.config, env=env, provider=provider, model=model)
        self._extension_paths = extension_paths
        self.pi = PiRpcClient(config)
        self.runtime.run_async(
            self.pi.start(extension_paths=extension_paths),
            timeout=max(10.0, config.startup_timeout + 5.0),
        )
        return self.snapshot()

    def close(self) -> None:
        self._check_owner()
        pi = self.pi
        server = self.server
        self.pi = None
        self.server = None
        if pi is not None:
            self.runtime.run_async(pi.close(), timeout=10.0)
        if server is not None:
            self.runtime.run_async(server.close(), timeout=10.0)
        if self._tempdir is not None and not self.keep_temp:
            self._tempdir.cleanup()
        self._tempdir = None
        self.endpoint = None
        self._extension_paths = []

    def call_rpc(
        self, method_name: str, *args: Any, wait_timeout: float | None = None, **kwargs: Any
    ) -> Any:
        self._check_owner()
        if self.pi is None:
            raise RuntimeError("PiAgentHarness is not started")
        method = getattr(self.pi, method_name)
        return self.runtime.run_async(method(*args, **kwargs), timeout=wait_timeout)

    def subscribe_client_events(
        self, handler: Callable[[dict[str, Any]], Any]
    ) -> Callable[[], None]:
        self._check_owner()
        if self.pi is None:
            raise RuntimeError("PiAgentHarness is not started")

        async def install() -> Callable[[], None]:
            assert self.pi is not None
            return self.pi.on_event(handler)

        return cast(Callable[[], None], self.runtime.run_async(install(), timeout=5.0))

    def unsubscribe_client_events(self, unsubscribe: Callable[[], None]) -> None:
        self._check_owner()

        async def uninstall() -> None:
            unsubscribe()

        self.runtime.run_async(uninstall(), timeout=5.0)

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
