from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

from pi_python_harness import HarnessRuntime, PiAgentHarness, PiLaunchConfig, ToolRegistry
from pi_python_harness.jsonl import dumps_line
from pi_python_harness.server import PythonToolServer


def fake_config() -> PiLaunchConfig:
    script = Path(__file__).with_name("fake_pi_rpc.py")
    return PiLaunchConfig(
        pi_command=[sys.executable, str(script)],
        request_timeout=5,
        startup_timeout=0.2,
        offline=False,
        no_extensions=False,
        no_skills=False,
        no_prompt_templates=False,
        no_context_files=False,
    )


def test_runtime_loop_thread_is_not_main_thread() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-Thread-2", tool_thread_name_prefix="test-pi-tool")
    try:
        main_id = threading.get_ident()

        async def identify() -> tuple[int, str]:
            return threading.get_ident(), threading.current_thread().name

        loop_id, loop_name = runtime.run_async(identify())
        assert loop_id != main_id
        assert loop_name == "PiAsyncioLoop-Thread-2"
    finally:
        runtime.close()


def test_one_harness_can_live_on_main_thread_for_tests() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-Thread-2-test-main", tool_thread_name_prefix="test-pi-tool")
    harness = PiAgentHarness(ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False)
    try:
        assert harness.owner_thread_id == threading.get_ident()
        harness.start()
        snapshot = harness.snapshot()
        assert snapshot.owner_thread_id == threading.get_ident()
        assert snapshot.owner_thread_name == threading.current_thread().name
        assert snapshot.async_loop_thread_id != threading.get_ident()
        assert snapshot.registry_frozen is True
        assert harness.get_state(timeout=5)["sessionId"] == "fake"
    finally:
        harness.close()
        runtime.close()


def test_multiple_harnesses_have_distinct_owner_threads_and_shared_loop() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-Thread-2-shared", tool_thread_name_prefix="shared-pi-tool")
    h1 = PiAgentHarness(ToolRegistry(), config=fake_config(), runtime=runtime, owner_thread_name="PiAgentHarness-one")
    h2 = PiAgentHarness(ToolRegistry(), config=fake_config(), runtime=runtime, owner_thread_name="PiAgentHarness-two")
    try:
        h1.start()
        h2.start()
        s1 = h1.snapshot()
        s2 = h2.snapshot()
        main_id = threading.get_ident()

        assert s1.owner_thread_id != main_id
        assert s2.owner_thread_id != main_id
        assert s1.owner_thread_id != s2.owner_thread_id
        assert s1.async_loop_thread_id == s2.async_loop_thread_id
        assert s1.async_loop_thread_name == "PiAsyncioLoop-Thread-2-shared"
        assert h1.get_state(timeout=5)["sessionId"] == "fake"
        assert h2.get_state(timeout=5)["sessionId"] == "fake"
    finally:
        h1.close()
        h2.close()
        runtime.close()


async def _read_frame(reader: asyncio.StreamReader) -> dict:
    return json.loads((await reader.readline()).decode("utf-8"))


async def _execute(endpoint, tool: str, params: dict) -> dict:
    reader, writer = await asyncio.open_connection(endpoint.host, endpoint.port)
    writer.write(dumps_line({"id": tool, "type": "execute", "token": endpoint.token, "tool": tool, "params": params}))
    await writer.drain()
    response = await _read_frame(reader)
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.asyncio
async def test_tool_calls_for_multiple_registries_use_shared_threadpool() -> None:
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-Thread-2-tools", tool_thread_name_prefix="shared-pi-tool")
    registry_a = ToolRegistry()
    registry_b = ToolRegistry()

    @registry_a.register(description="identify A")
    def identify_a() -> dict[str, str]:
        return {"registry": "a", "thread": threading.current_thread().name}

    @registry_b.register(description="identify B")
    def identify_b() -> dict[str, str]:
        return {"registry": "b", "thread": threading.current_thread().name}

    registry_a.freeze()
    registry_b.freeze()
    server_a = PythonToolServer(registry_a, tool_executor=runtime.tool_executor)
    server_b = PythonToolServer(registry_b, tool_executor=runtime.tool_executor)
    try:
        endpoint_a = runtime.run_async(server_a.start())
        endpoint_b = runtime.run_async(server_b.start())
        response_a, response_b = await asyncio.gather(
            _execute(endpoint_a, "identify_a", {}),
            _execute(endpoint_b, "identify_b", {}),
        )
        assert response_a["success"] is True
        assert response_b["success"] is True
        assert response_a["data"]["details"]["value"]["registry"] == "a"
        assert response_b["data"]["details"]["value"]["registry"] == "b"
        assert response_a["data"]["details"]["value"]["thread"].startswith("shared-pi-tool")
        assert response_b["data"]["details"]["value"]["thread"].startswith("shared-pi-tool")
    finally:
        runtime.run_async(server_a.close())
        runtime.run_async(server_b.close())
        runtime.close()
