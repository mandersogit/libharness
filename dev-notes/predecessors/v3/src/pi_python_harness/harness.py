"""High-level Python-first harness tying Pi RPC and Python tools together."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bridge import PythonToolServer
from .client import PiRpcClient
from .shim import write_shim
from .tools import ToolRegistry


@dataclass
class PythonPiHarness:
    """Launch Pi in RPC mode with Python-authored tools.

    The harness creates:

    1. a loopback Python tool server,
    2. a temporary JSON manifest of registered Python tools,
    3. a temporary TypeScript extension shim loaded by Pi using ``-e``, and
    4. a :class:`PiRpcClient` subprocess connected to ``pi --mode rpc``.
    """

    registry: ToolRegistry
    pi_command: list[str] = field(default_factory=lambda: ["pi"])
    cwd: str | Path | None = None
    provider: str | None = None
    model: str | None = None
    no_session: bool = False
    session_dir: str | Path | None = None
    extra_args: list[str] = field(default_factory=list)
    keep_temp: bool = False

    tool_server: PythonToolServer | None = field(default=None, init=False)
    client: PiRpcClient | None = field(default=None, init=False)
    _tempdir: tempfile.TemporaryDirectory[str] | None = field(default=None, init=False)
    manifest_path: Path | None = field(default=None, init=False)
    shim_path: Path | None = field(default=None, init=False)

    async def start(self) -> PiRpcClient:
        if self.client is not None:
            return self.client

        self._tempdir = tempfile.TemporaryDirectory(prefix="pi-python-harness-")
        temp_path = Path(self._tempdir.name)
        self.manifest_path = temp_path / "python-tools.json"
        self.shim_path = temp_path / "python-tool-bridge.ts"

        self.manifest_path.write_text(json.dumps(self.registry.manifest(), indent=2), encoding="utf-8")
        write_shim(self.shim_path)

        self.tool_server = PythonToolServer(self.registry)
        endpoint = await self.tool_server.start()

        env = endpoint.env()
        env["PI_PY_TOOL_MANIFEST"] = str(self.manifest_path)

        extra_args = ["--extension", str(self.shim_path), *self.extra_args]
        self.client = PiRpcClient(
            command=self.pi_command,
            cwd=self.cwd,
            env=env,
            provider=self.provider,
            model=self.model,
            no_session=self.no_session,
            session_dir=self.session_dir,
            extra_args=extra_args,
        )
        await self.client.start()
        return self.client

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self.tool_server is not None:
            await self.tool_server.close()
            self.tool_server = None
        if self._tempdir is not None and not self.keep_temp:
            self._tempdir.cleanup()
            self._tempdir = None

    async def __aenter__(self) -> PiRpcClient:
        return await self.start()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def prompt(self, message: str, **kwargs: Any) -> None:
        client = self.client or await self.start()
        await client.prompt(message, **kwargs)
