# Best Practices for Python-First Pi Harnesses

## Architecture

- Make Python the parent process.
- Start Pi in RPC mode.
- Use one generic TypeScript bridge extension.
- Register Python tools through Pi's native `registerTool()` path.
- Keep fake providers and test doubles outside the production bridge.

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
