from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .broker import PythonToolBroker
from .rpc import PiRpcClient
from .shim import GeneratedShim, write_generated_shim
from .tools import ToolRegistry
from .types import PiLaunchOptions


class PiPythonHarness:
    """High-level owner for the Python broker, generated TS shim, and Pi RPC client."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        launch_options: PiLaunchOptions | None = None,
        workdir: str | Path | None = None,
    ) -> None:
        self.registry = registry or ToolRegistry()
        self.launch_options = launch_options or PiLaunchOptions()
        self.workdir = Path(workdir) if workdir is not None else None
        self.broker = PythonToolBroker(self.registry)
        self.generated: GeneratedShim | None = None
        self.client: PiRpcClient | None = None

    async def start(self) -> PiRpcClient:
        handle = self.broker.start()
        self.generated = write_generated_shim(self.registry, bridge_url=handle.url, token=handle.token, directory=self.workdir)
        env = dict(self.generated.env)
        self.client = PiRpcClient.from_launch_options(
            self.launch_options,
            extension_path=str(self.generated.extension_path),
            env=env,
        )
        await self.client.start()
        return self.client

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.stop()
            self.client = None
        self.broker.stop()

    async def __aenter__(self) -> PiRpcClient:
        return await self.start()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.stop()

    def generated_env(self) -> Mapping[str, str]:
        if self.generated is None:
            raise RuntimeError("Harness has not generated a shim yet")
        return self.generated.env
