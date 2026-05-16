from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from pi_python_harness import (
    PiLaunchConfig,
    PiRpcClient,
    write_faux_toolcall_provider_extension,
    write_python_tool_shim,
)


def pi_command_from_env() -> list[str] | str | None:
    cli = os.environ.get("PI_CLI")
    if cli:
        return ["node", cli]
    return os.environ.get("PI_COMMAND")


@unittest.skipUnless(pi_command_from_env(), "set PI_CLI or PI_COMMAND to run real Pi integration tests")
class RealPiRpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_state_from_real_pi_rpc_mode(self) -> None:
        config = PiLaunchConfig(
            pi_command=pi_command_from_env(),
            extra_args=["--no-session", "--offline", "--no-extensions"],
        )
        async with PiRpcClient(config) as client:
            state = await client.get_state()
        self.assertIn("sessionId", state)
        self.assertFalse(state["isStreaming"])

    async def test_python_tool_bridge_loaded_and_executed_by_faux_provider(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shim = write_python_tool_shim(
                tmp / "python-tools.ts",
                tool_server_command=[sys.executable, str(root / "examples" / "echo_tools.py")],
                env={"PYTHONPATH": str(root)},
            )
            faux = write_faux_toolcall_provider_extension(
                tmp / "faux-provider.ts",
                tool_name="py_echo",
                arguments={"message": "hello"},
                final_text="finished",
            )
            config = PiLaunchConfig(
                pi_command=pi_command_from_env(),
                provider="pyharness-test",
                model="pyharness-faux-1",
                env={"PYHARNESS_FAUX_API_KEY": "test"},
                cwd=root,
                extra_args=[
                    "--no-session",
                    "--offline",
                    "--extension",
                    str(shim),
                    "--extension",
                    str(faux),
                    "--tools",
                    "py_echo",
                ],
                request_timeout=45,
            )
            async with PiRpcClient(config) as client:
                commands = await client.get_commands()
                self.assertTrue(any(command["name"] == "python-tools" for command in commands))
                events = await client.prompt_and_wait("exercise the python tool", timeout=60)
            tool_end = [event for event in events if event.get("type") == "tool_execution_end"]
            self.assertTrue(tool_end, events)
            self.assertEqual(tool_end[0]["toolName"], "py_echo")
            self.assertIn("python echo: hello", tool_end[0]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
