from __future__ import annotations

import asyncio
import json
import os
import threading
import typing as t
from dataclasses import dataclass, field

from .client import PiRpcClient
from .server import PythonToolServer
from .shim import default_shim_path
from .tools import ToolRegistry


@dataclass
class PiPythonHarnessConfig:
    pi_command: list[str] = field(default_factory=lambda: ["pi"])
    cwd: str | None = None
    provider: str = "google"
    model: str = "google/gemini-2.5-pro"
    offline: bool = True
    no_session: bool = True
    no_discovery_extensions: bool = True
    no_skills: bool = True
    no_prompt_templates: bool = True
    no_context_files: bool = True
    extra_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    shim_path: str | None = None
    stderr: t.Literal["pipe", "inherit", "devnull"] = "pipe"
    fake_provider: bool = False
    fake_tool_name: str | None = None
    fake_tool_args: dict[str, t.Any] | None = None


class _ServerThread:
    def __init__(self, server: PythonToolServer) -> None:
        self.server = server
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._started = threading.Event()
        self._error: BaseException | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="py-pi-tool-server", daemon=True)
        self.thread.start()
        self._started.wait(timeout=10)
        if self._error:
            raise RuntimeError("Failed to start Python tool server") from self._error
        if self.loop is None:
            raise RuntimeError("Python tool server did not start")

    def stop(self) -> None:
        if self.loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop)
        try:
            fut.result(timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            if self.thread:
                self.thread.join(timeout=5)

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.server.start())
            self._started.set()
            self.loop.run_forever()
        except BaseException as exc:
            self._error = exc
            self._started.set()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()


class PiPythonHarness:
    """Owns a Python tool server and a Pi RPC subprocess."""

    def __init__(self, registry: ToolRegistry, config: PiPythonHarnessConfig | None = None, **config_overrides: t.Any) -> None:
        self.registry = registry
        base = config or PiPythonHarnessConfig()
        for key, value in config_overrides.items():
            if not hasattr(base, key):
                raise TypeError(f"Unknown PiPythonHarnessConfig field: {key}")
            setattr(base, key, value)
        self.config = base
        self.server = PythonToolServer(registry)
        self._server_thread = _ServerThread(self.server)
        self.client: PiRpcClient | None = None

    def __enter__(self) -> "PiPythonHarness":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def start(self) -> "PiPythonHarness":
        self._server_thread.start()
        host, port = self.server.address
        shim_path = self.config.shim_path or str(default_shim_path())
        args = [*self.config.pi_command, "--mode", "rpc", "--extension", shim_path]
        if self.config.provider:
            args.extend(["--provider", self.config.provider])
        if self.config.model:
            args.extend(["--model", self.config.model])
        if self.config.offline:
            args.append("--offline")
        if self.config.no_session:
            args.append("--no-session")
        if self.config.no_discovery_extensions:
            args.append("--no-extensions")
        if self.config.no_skills:
            args.append("--no-skills")
        if self.config.no_prompt_templates:
            args.append("--no-prompt-templates")
        if self.config.no_context_files:
            args.append("--no-context-files")
        args.extend(self.config.extra_args)

        env = {
            **self.config.env,
            "PY_PI_TOOLS_HOST": host,
            "PY_PI_TOOLS_PORT": str(port),
            "PY_PI_TOOLS_TOKEN": self.server.token or "",
            "PY_PI_FAKE_PROVIDER": "1" if self.config.fake_provider else "0",
        }
        if self.config.fake_tool_name:
            env["PY_PI_FAKE_TOOL_NAME"] = self.config.fake_tool_name
        if self.config.fake_tool_args is not None:
            env["PY_PI_FAKE_TOOL_ARGS"] = json.dumps(self.config.fake_tool_args, separators=(",", ":"))
        # Used by the shim-registered provider; it must resolve to a non-empty auth value.
        env.setdefault("PY_PI_FAKE_API_KEY", "dummy")
        env.setdefault("PI_OFFLINE", "1" if self.config.offline else "0")

        self.client = PiRpcClient(args, cwd=self.config.cwd, env=env, stderr=self.config.stderr).start()
        return self

    def close(self) -> None:
        try:
            if self.client is not None:
                self.client.close()
        finally:
            self._server_thread.stop()
            self.client = None
