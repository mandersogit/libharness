"""Optional integration tests against a built Pi checkout.

Run manually, for example:

    PI_CLI="node /mnt/data/pi_src/pi-main/packages/coding-agent/dist/cli.js" \
    PI_CWD="/mnt/data/pi_src/pi-main" \
    PYTHONPATH=src python -m pytest tests/test_pi_integration_manual.py -q
"""

import os
import shlex

import pytest

from py_pi_harness import PiPythonHarness, ToolRegistry


def _pi_command():
    cmd = os.environ.get("PI_CLI")
    if not cmd:
        pytest.skip("Set PI_CLI to run Pi integration tests")
    return shlex.split(cmd)


def test_pi_loads_python_tool_shim_and_manual_command():
    registry = ToolRegistry()

    @registry.tool(description="Echo text")
    def echo(text: str) -> str:
        return "echo:" + text

    with PiPythonHarness(
        registry,
        pi_command=_pi_command(),
        cwd=os.environ.get("PI_CWD"),
    ) as harness:
        assert harness.client is not None
        commands = harness.client.get_commands(timeout=20)
        assert {cmd["name"] for cmd in commands if cmd["name"].startswith("py")} >= {"py-tools", "py-tool"}
        harness.client.prompt('/py-tool echo {"text":"hi"}', timeout=20)
        event = harness.client.wait_event(
            lambda e: e.get("type") == "extension_ui_request" and e.get("method") == "notify",
            timeout=20,
        )
        assert event["message"] == "echo:hi"


def test_pi_fake_provider_executes_python_tool_loop():
    registry = ToolRegistry()

    @registry.tool(description="Echo text")
    def echo(text: str) -> str:
        return "echo:" + text

    with PiPythonHarness(
        registry,
        pi_command=_pi_command(),
        cwd=os.environ.get("PI_CWD"),
        fake_provider=True,
        fake_tool_name="echo",
        fake_tool_args={"text": "sample"},
    ) as harness:
        assert harness.client is not None
        harness.client.set_model("py-pi-fake", "toolcaller", timeout=20)
        harness.client.prompt("exercise the Python tool", timeout=20)
        harness.client.wait_event(lambda e: e.get("type") == "agent_end", timeout=30)
        text = harness.client.send({"type": "get_last_assistant_text"}, timeout=10)["data"]["text"]
        assert "echo:sample" in text
