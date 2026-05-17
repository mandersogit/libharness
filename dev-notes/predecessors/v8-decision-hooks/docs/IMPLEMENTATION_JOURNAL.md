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


## Agent hook pass

This pass added the subclassable `Agent` layer requested after the thread-owned reshape.

Changes made:

1. Added `src/pi_python_harness/agent_class.py` with `Agent`, `AgentEvent`, and
   `UnhandledEventError`.
2. Kept `Agent` as an inheritance-based API: `class Agent(PiAgentHarness)`.
3. Added the parallel `async_on_*` / `on_*` hook pattern with class-construction
   validation for unsupported suffixes and both-defined collisions.
4. Added `HarnessRuntime.hook_executor`, a dedicated single-worker executor for sync
   hooks, separate from the shared tool executor.
5. Wired `Agent.start()` to subscribe one additional handler to `PiRpcClient.on_event()`
   after the Pi client exists.
6. Kept lower-level raw event access intact through `PiRpcClient.on_event()` and
   `PiRpcClient.next_event()`.
7. Added the focused core agent-loop named event set from Pi source rather than exposing
   all session/RPC/extension events.
8. Used a frozen `AgentEvent` wrapper around raw dict payloads instead of one typed
   dataclass per event.
9. Changed `PiRpcClient.set_model()` to send `modelId`, matching Pi's RPC type.
10. Added `protocolVersion = 1` to the Python bridge manifest and made the TypeScript
    shim assert it.

Judgment calls:

- Event set: named hooks cover only the ten low-level agent-loop events. Other emitted
  events remain lower-level.
- Relationship: inheritance won for v1 because it gives a single object with
  `start()` / `prompt()` / hooks.
- Payload: frozen wrapper over raw event dictionaries, not per-event dataclasses.
- Lifecycle: subscribe during `Agent.start()` after the `PiRpcClient` is created and
  started; unsubscribe during `Agent.close()` before closing the harness.

Commands run in this pass:

```bash
python -m ruff check .
# All checks passed!

python -m mypy
# Success: no issues found in 11 source files

python -m pyright
# 0 errors, 0 warnings, 0 informations

python -m pytest -q
# 18 passed, 1 skipped in 17.08s

python -m compileall -q src tests
# passed
```

## v8 decision-hook pass

This pass adds Python participation in Pi's in-process extension event surface while keeping the
previous architecture intact: `HarnessRuntime`, `PiAgentHarness`, `Agent(PiAgentHarness)`, one shared
asyncio loop thread, one owner thread per normal harness, a shared tool executor, and a dedicated hook
executor.

Changes made:

1. Expanded `Agent` to include the 18 RPC notification events: the ten core agent-loop events, seven
   session-layer events, and `extension_error`.
2. Added the 19 in-process extension/decision events from Pi's `ExtensionAPI.on(...)` surface.
3. Added `decide_X` / `async_decide_X` hooks for all 19 decision events.
4. Added bridge observation for decision events through `on_X` / `async_on_X`; observation runs before
   decision when both are present.
5. Added `HookContext` with cooperative `cancelled` polling for decision hooks.
6. Added decision timeout manifest plumbing via `_decision_timeout_ms` and `_decision_timeouts_ms`.
7. Promoted `MANIFEST_PROTOCOL_VERSION = 1` to a named constant in `tools.py`.
8. Made `AgentEvent.payload` a shallow immutable mapping.
9. Extended the TypeScript shim to subscribe to all decision events, use `notify_event` for gate-closed
   events, use `event` for gate-open events, and fail open on hook errors/timeouts.
10. Extended `PythonToolServer` to handle `notify_event` and `event` bridge requests.
11. Preserved channel separation: bridge decision events are not injected into `PiRpcClient.on_event()`
    or `next_event()`.
12. Added exception isolation around notification hooks and decision hooks.
13. Added examples for basic notification hooks and a bash permission gate.
14. Added a Pi source taxonomy regression test that skips when the Pi source tree is not available.

Judgment calls:

- **Cancellation API:** decision hooks may accept `ctx: HookContext` as a second positional argument.
  This mirrors tool-side cooperative cancellation without requiring every hook to accept context.
- **Timeout configuration:** both `_decision_timeout_ms` and `_decision_timeouts_ms` are supported.
  Per-event values override the global value. Default remains no timeout.
- **Module organization:** event and decision machinery remain in `agent_class.py` because the classes
  are tightly coupled and the public surface is still small.
- **Observation hooks for decision events:** the requested always-notify model and same-event
  observe-then-decide behavior require `on_<decision_event>` to be accepted. The implementation keeps
  `_EVENT_NAMES` as the 18 RPC notification events and `_DECISION_EVENT_NAMES` as the 19 in-process
  extension events, while validation accepts observation hooks for the union.

Commands run in this pass:

```bash
python -m ruff check .
# All checks passed!

python -m mypy
# Success: no issues found in 11 source files

python -m pyright
# 0 errors, 0 warnings, 0 informations

python -m pytest -q
# 36 passed, 1 skipped in 8.03s

python -m compileall -q src tests examples
# passed
```

I also ran a local 100-column scan over all Python source and test files, and it found no line-length
violations.
