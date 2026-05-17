# Implementation Journal

## Review work performed

1. Unpacked the original Pi repository and four generated artifacts.
2. Inspected Pi source files for RPC command support, extension/tool registration, TypeBox/JSON Schema validation behavior, CLI flags, and agent/tool execution flow.
3. Queried current public Pi documentation to cross-check source findings.
4. Ran each generated artifact's tests.
5. Installed and ran the published `@earendil-works/pi-coding-agent@0.74.0` package.
6. Verified Pi RPC with `get_state` in this environment.
7. Built a synthesized Python package combining the best implementation traits.
8. Added unit tests, fake-RPC tests, and real-Pi/faux-provider integration tests.
9. Ran all synthesized tests successfully.

## Important correction to generated attempts

Several attempts claimed Pi could not be meaningfully run in the environment because of Node constraints. In this review environment, Node 22 and npm were available. The direct uploaded monorepo build was not completed because npm workspace installation behaved inconsistently in the sandbox, but the published Pi package at the matching current package version installed and ran. That was sufficient to test RPC mode and the tool bridge.

## Commands run for synthesized implementation

```bash
cd /mnt/data/pi_synthesis/pi-python-harness-synthesis
python -m pytest -q
# 6 passed, 1 skipped

PI_CLI=/mnt/data/pi_pkg/node_modules/@earendil-works/pi-coding-agent/dist/cli.js python -m pytest -q
# 7 passed
```

## Files produced

- `src/pi_python_harness/jsonl.py`: strict byte-oriented JSONL helpers.
- `src/pi_python_harness/tools.py`: registry, schema inference, `ToolContext`, `ToolResult`.
- `src/pi_python_harness/server.py`: async local bridge server.
- `src/pi_python_harness/rpc.py`: async Pi RPC client with headless extension UI handling.
- `src/pi_python_harness/shim.py`: production TS bridge writer and separate faux-provider writer.
- `src/pi_python_harness/harness.py`: high-level lifecycle orchestration.
- `tests/`: unit, fake-RPC, and optional real-Pi integration tests.
- `docs/`: audit, design, best practices, source findings, and run results.

## Thread-owned reshaping pass

A second implementation pass reshaped the package around explicit thread ownership:

1. Added `src/pi_python_harness/runtime.py` with `AsyncioLoopThread`, `HarnessRuntime`, and default-runtime helpers.
2. Added `src/pi_python_harness/agent.py` with `PiAgentHarness`, `HarnessSnapshot`, and an owner-thread-only `_PiAgentHarnessCore`.
3. Modified `PythonToolServer` to accept a shared `tool_executor` and run tool calls in that executor.
4. Modified `ToolRegistry` to use a lock and support `freeze()` so registries become stable before worker-thread reads.
5. Updated package exports to expose `PiAgentHarness`, `HarnessRuntime`, and related runtime helpers.
6. Added tests for the dedicated asyncio loop thread, main-thread test mode, multiple owner-thread harnesses, and shared thread-pool tool execution.
7. Updated README and design documentation to describe the new threading model.

Commands run after the reshaping:

```bash
cd /mnt/data/pi_synth_work/pi-python-harness-synthesis
pytest -q tests/test_threaded_agent.py -q
# .... [100%]

pytest -q
# ..s........ [100%]
# 10 passed, 1 skipped in 5.86s
```

New files produced:

- `src/pi_python_harness/runtime.py`: dedicated asyncio loop thread and shared runtime.
- `src/pi_python_harness/agent.py`: synchronous owner-thread harness facade.
- `tests/test_threaded_agent.py`: thread-affinity and shared-tool-pool tests.
- `docs/THREADING_MODEL.md`: detailed threading contract and lifecycle.
