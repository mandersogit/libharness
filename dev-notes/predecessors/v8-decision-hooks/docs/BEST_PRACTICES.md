# Best Practices for Python-First Pi Harnesses

## Architecture

- Keep Python as the parent process.
- Start Pi in RPC mode.
- Use one generic TypeScript bridge extension.
- Register Python tools through Pi's native `registerTool()` path.
- Keep fake providers and test doubles outside the production bridge.
- Use `PiAgentHarness` for application embedding and reserve `PiPythonHarness` for async-first experiments.

## Threading

- Keep `MainThread` for the application.
- Use one shared `HarnessRuntime` for related harnesses.
- Let `HarnessRuntime` own the dedicated asyncio loop thread.
- Run each normal `PiAgentHarness` on its own owner thread.
- Use `threaded=False` only for single-harness tests or deliberately synchronous experiments.
- Freeze each tool registry before exposing it to Pi.
- Execute tool calls through a shared `ThreadPoolExecutor` so concurrency limits are centralized.
- Avoid mutating application state directly from tools unless that state is explicitly synchronized.

## Launch flags

Recommended deterministic defaults:

```text
--mode rpc
--offline
--no-session
--no-extensions
--no-skills
--no-prompt-templates
--no-context-files
--extension <python_tools_extension.ts>
```

Use `--no-builtin-tools` when the run should expose only Python-provided tools. Do not use `--no-tools` for this mode because it can disable custom tools as well.

## Tool design

- Prefer idempotent tools where possible.
- Use explicit JSON Schema for public/external tools.
- Use type-hint inference for local/internal convenience, then inspect the manifest.
- Mark tools sequential or protect mutable shared state when needed.
- Return `ToolResult` for precise content/details/termination control.
- Use `ToolContext.update()` for streaming progress.
- Check `ToolContext.cancelled` in long-running tools.
- Keep blocking CPU or I/O inside tool functions acceptable to run on pool threads.
- For async tools, the bridge executes the coroutine inside the tool worker thread, not on the shared Pi I/O loop.

## RPC and UI

- Treat Pi RPC prompt responses as acknowledgement, not completion. Listen for events such as `agent_end`.
- Auto-handle extension UI requests in headless runs.
- Preserve stderr for debugging failed startup or tool-loop errors.
- Use byte-oriented JSONL parsing.

## Security

- Assume Pi extensions and Python tools have process-level privileges.
- Use the bridge token as a guardrail, not a sandbox.
- For untrusted code, run tools out of process with OS-level isolation.
- Pin Pi versions and audit npm transitive dependencies before workplace deployment.
