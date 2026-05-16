from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from pi_python_harness import PiLaunchConfig, PiRpcClient, write_python_tool_shim


async def main() -> None:
    # Set PI_CLI to an installed Pi CLI JS file, or leave unset to use `pi` from PATH.
    pi_cli = os.environ.get("PI_CLI")
    pi_command = ["node", pi_cli] if pi_cli else "pi"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(__file__).resolve().parents[1]
        shim = write_python_tool_shim(
            Path(tmp) / "python-tools.ts",
            tool_server_command=[sys.executable, str(Path(__file__).with_name("echo_tools.py"))],
            # Helpful when running from this source checkout rather than an installed wheel.
            env={"PYTHONPATH": str(root)},
        )
        config = PiLaunchConfig(
            pi_command=pi_command,
            extra_args=["--no-session", "--offline", "--extension", str(shim)],
        )
        async with PiRpcClient(config) as client:
            print(await client.get_state())
            print(await client.get_commands())


if __name__ == "__main__":
    asyncio.run(main())
