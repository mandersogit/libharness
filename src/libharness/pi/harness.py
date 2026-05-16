"""High-level Python-owned lifecycle for Pi RPC plus Python tools."""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .rpc import PiLaunchConfig, PiRpcClient
from .server import PythonToolServer
from .shim import write_bridge_shim, write_faux_toolcall_provider_extension
from .tools import ToolRegistry


class PiPythonHarness:
    """Start Pi in RPC mode with Python-authored tools.

    Python owns the process tree. The TypeScript code is a generic adapter only;
    it contains no user business logic and no per-tool customization.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        config: PiLaunchConfig | None = None,
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
        bridge_max_handlers: int = 256,
        bridge_write_timeout: float | None = None,
    ) -> None:
        self.registry = registry
        self.config = config or PiLaunchConfig()
        self.workdir = Path(workdir) if workdir is not None else None
        self.shim_path = Path(shim_path) if shim_path is not None else None
        self.diagnostic_commands = diagnostic_commands
        self.keep_temp = keep_temp
        self.fake_provider = fake_provider
        self.fake_tool_name = fake_tool_name
        self.fake_tool_args = fake_tool_args or {}
        self.fake_provider_name = fake_provider_name
        self.fake_model_id = fake_model_id
        self.fake_final_text = fake_final_text

        self.server = PythonToolServer(
            registry,
            max_handlers=bridge_max_handlers,
            bridge_write_timeout=bridge_write_timeout,
        )
        self.pi: PiRpcClient | None = None
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._extension_paths: list[Path] = []

    async def __aenter__(self) -> PiPythonHarness:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def start(self) -> PiPythonHarness:
        if self.pi is not None:
            raise RuntimeError("PiPythonHarness is already started")

        endpoint = await self.server.start()
        root = self._artifact_root()
        shim_path = self.shim_path or root / "python_tools_extension.ts"
        write_bridge_shim(shim_path, diagnostic_commands=self.diagnostic_commands)
        extension_paths = [shim_path]

        provider = self.config.provider
        model = self.config.model
        env = dict(self.config.env or {})
        env.update(endpoint.env())
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        env.setdefault("PI_OFFLINE", "1")
        env.setdefault("PI_PY_DIAGNOSTIC_COMMANDS", "1" if self.diagnostic_commands else "0")

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
        await self.pi.start(extension_paths=extension_paths)
        return self

    async def close(self) -> None:
        if self.pi is not None:
            await self.pi.close()
            self.pi = None
        await self.server.close()
        if self._tempdir is not None and not self.keep_temp:
            self._tempdir.cleanup()
        self._tempdir = None

    @property
    def extension_paths(self) -> Sequence[Path]:
        return tuple(self._extension_paths)

    def _artifact_root(self) -> Path:
        if self.workdir is not None:
            self.workdir.mkdir(parents=True, exist_ok=True)
            return self.workdir
        if self._tempdir is None:
            self._tempdir = tempfile.TemporaryDirectory(prefix="pi-python-harness-")
        return Path(self._tempdir.name)

    @property
    def client(self) -> PiRpcClient:
        if self.pi is None:
            raise RuntimeError("PiPythonHarness is not started")
        return self.pi


def _first_tool_name(registry: ToolRegistry) -> str:
    for registered in registry:
        return registered.spec.name
    raise RuntimeError("fake_provider requires at least one registered Python tool")
