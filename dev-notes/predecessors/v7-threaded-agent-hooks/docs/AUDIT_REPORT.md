# Audit, Comparison, and Synthesis Report

## Scope

This report audits four independently generated results for using Pi as a Python-first agentic harness. It compares them against:

- the uploaded Pi source tree (`pi-main(4).zip`), especially the RPC implementation, extension API, tool registration types, CLI flags, and agent/tool execution model;
- the public Pi documentation available at review time;
- empirical tests run in this environment against each artifact and against the synthesized implementation.

The report labels the four attempts as follows:

| Label | Uploaded artifact | Short description |
|---|---|---|
| A | `pi_python_harness_artifacts(1).zip` | Python parent, Pi RPC subprocess, generated TS extension, Python HTTP/NDJSON tool broker. |
| B | `pi-python-harness.zip` | Python parent, Pi RPC subprocess, generated TS extension, async TCP JSONL tool server. |
| C | `pi_python_harness_artifacts.zip` | TS extension spawns a Python tool server over stdio; includes real-Pi/faux-provider integration tests. |
| D | `pi_python_harness.zip` | Python parent, async TCP server, static packaged TS extension, rich type-inferred registry, optional fake provider in shim. |

## Executive verdict

The best final architecture is a refinement of the Opus-style proposal:

> Python owns orchestration and lifecycle; Pi runs as an RPC subprocess; a very small generic TypeScript extension registers Python-backed tools; Python hosts the tool registry and tool execution bridge.

That recommendation is strongly supported by the Pi source and docs. Pi exposes RPC mode for embedding and a TypeScript extension API for registering tools. The source does not expose a dynamic RPC command for registering arbitrary tools at runtime, so Python-authored tools need an extension-side registration point unless Pi is forked.

The strongest individual attempts were:

- **B for RPC client design**: async subprocess client, strict JSONL framing, and automatic headless extension-UI handling.
- **D for tool authoring ergonomics and deterministic launch defaults**: decorator registration, schema inference, async tools, streaming updates, and safer CLI flags.
- **C for integration testing discipline**: a separate faux-provider extension exercises Pi's actual tool loop without an LLM.
- **A for clear architectural assessment and a useful HTTP broker prototype**, though its runability claims are stale.

The synthesized implementation included with this report combines those strengths and intentionally rejects their weaker choices.

## Empirical test results

### Original attempts

Tests were run as provided, and then with `PYTHONPATH=src` where applicable.

| Attempt | Plain `python -m pytest -q` | With `PYTHONPATH=src` where applicable | Notes |
|---|---:|---:|---|
| A | Failed import collection | 5 passed | Source layout omitted pytest pythonpath configuration. Tests use fake RPC only. |
| B | Failed import collection | 7 passed | Same packaging/test-path issue. Tests use fake RPC only. |
| C | 3 passed, 2 skipped | Not needed | Optional real-Pi integration skipped unless `PI_CLI`/`PI_COMMAND` set. |
| D | 3 passed, 2 skipped | 3 passed, 2 skipped | Optional real-Pi integration skipped unless `PI_CLI` set. |

### Synthesized implementation

The synthesized implementation was tested in this environment:

```text
python -m pytest -q
6 passed, 1 skipped in 1.38s

PI_CLI=/mnt/data/pi_pkg/node_modules/@earendil-works/pi-coding-agent/dist/cli.js python -m pytest -q
7 passed in 2.55s
```

The real-Pi integration path used published package `@earendil-works/pi-coding-agent@0.74.0` and a test-only faux-provider extension. It verified:

1. Pi starts in RPC mode.
2. The generated TypeScript bridge extension loads.
3. The Python tool bridge exposes the manifest.
4. Diagnostic commands are registered.
5. A faux provider emits a tool call.
6. Pi executes a Python tool through its real tool execution loop.
7. The agent reaches `agent_end` without an external LLM.

## Source-grounded constraints

The uploaded Pi source establishes several important constraints:

1. **RPC is an embedding/control interface, not a full runtime extension API.**  
   `packages/coding-agent/src/modes/rpc/rpc-types.ts` defines commands such as `prompt`, `steer`, `follow_up`, `abort`, `new_session`, `get_state`, `get_messages`, `get_commands`, `bash`, and extension UI responses. It does not define a `register_tool` RPC command.

2. **Tools are registered through the extension API.**  
   `packages/coding-agent/src/core/extensions/types.ts` defines `ExtensionAPI.registerTool()` and `ToolDefinition.execute(...)`. That is the natural registration seam for custom tools.

3. **Extensions are TypeScript modules.**  
   Pi's extension loader expects TypeScript/JavaScript modules. This makes a zero-TypeScript solution unrealistic without forking Pi or waiting for an upstream non-TS extension API.

4. **TypeBox is native, but JSON Schema can be bridged carefully.**  
   Pi's validation utility has a JSON-Schema-compatible path when TypeBox metadata is absent. A bridge can therefore pass Python-produced JSON Schema through `Type.Unsafe(schema)` in the shim, but schemas should stay in a conservative JSON Schema subset and be tested.

5. **Explicit extension loading can coexist with disabling ambient resources.**  
   The CLI supports flags such as `--no-extensions` plus explicit `--extension` paths, making deterministic launch possible: disable ambient/local/global extensions and load exactly the bridge extension.

6. **Tool execution may be parallel by default.**  
   The bridge and Python server need to be concurrency-safe. Stateful tools should request sequential execution or protect mutable state.

## Comparative scoring

Scale: 1 = poor, 5 = excellent.

| Dimension | A | B | C | D | Synthesis |
|---|---:|---:|---:|---:|---:|
| Correct high-level architecture | 4 | 5 | 3 | 5 | 5 |
| Python-first lifecycle ownership | 4 | 5 | 2 | 5 | 5 |
| Minimal TypeScript surface | 4 | 4 | 4 | 3 | 5 |
| RPC client robustness | 4 | 5 | 3 | 3 | 5 |
| Headless extension-UI handling | 2 | 5 | 4 | 2 | 5 |
| Tool authoring ergonomics | 5 | 3 | 3 | 5 | 5 |
| Async/streaming tool support | 4 | 5 | 2 | 5 | 5 |
| Cancellation propagation | 3 | 4 | 2 | 4 | 4 |
| Deterministic Pi launch defaults | 4 | 3 | 3 | 5 | 5 |
| Integration testing against real Pi | 1 | 1 | 5 | 5 | 5 |
| Packaging/test runability | 3 | 3 | 5 | 4 | 5 |
| Documentation/source grounding | 4 | 5 | 4 | 4 | 5 |
| Security/trust-boundary handling | 3 | 3 | 3 | 4 | 4 |

## Attempt-by-attempt audit

### Attempt A

**Best contributions**

- Good architectural recommendation: Python parent process, Pi RPC, TS bridge, Python tool broker.
- Ergonomic decorator-based registry with type-hint schema inference.
- Strict byte-oriented JSONL client for Pi RPC.
- `ToolResult` and update plumbing are useful abstractions.
- Clear design and source-evidence documents.

**Problems**

- The artifact states that the environment could not meaningfully run Pi because of Node constraints. That is stale for this review environment; Node 22 is available and the published Pi package runs.
- Tests fail as packaged unless `PYTHONPATH=src` is supplied.
- The HTTP/`ThreadingHTTPServer` bridge is less appropriate than an async TCP/JSONL server for an async harness.
- Headless extension UI handling is manual and easy to forget. A blocking extension UI request can hang an RPC run.
- Unit tests use fake RPC only; they do not exercise Pi's real tool loop.

**Audit judgment**

A is a good conceptual starting point, but B/D/C each improve a key dimension: async bridge, deterministic launch, and real integration testing.

### Attempt B

**Best contributions**

- Strongest RPC client among the first two attempts.
- Proper strict JSONL parsing.
- Good lifecycle handling: close stdin, wait, then terminate if necessary.
- Automatic default responses for extension UI requests in headless mode.
- Async TCP JSONL bridge is cleaner than A's HTTP broker.
- Best design/runbook documentation among A and B.

**Problems**

- Tests fail as packaged unless `PYTHONPATH=src` is supplied.
- Python tool authoring is less ergonomic: explicit `(args, ctx)` handlers and explicit schemas dominate.
- Launch defaults do not sufficiently suppress ambient local/global Pi resources.
- No real Pi/faux-provider integration test.
- Some documentation repeats the stale claim that Pi could not be run in this environment.

**Audit judgment**

B provides the best RPC client baseline. The synthesis retains its async subprocess design and headless UI defaults, then adds D-style ergonomics and C-style integration tests.

### Attempt C

**Best contributions**

- Most valuable testing idea: a separate faux-provider extension that makes Pi call a tool without using a real LLM.
- Its tests can exercise Pi's actual tool execution loop when `PI_CLI`/`PI_COMMAND` is available.
- The top-level package layout runs tests as-is.
- Clear use of the published `@earendil-works/pi-coding-agent@0.74.0` package.

**Problems**

- The production architecture inverts lifecycle ownership: the TypeScript extension spawns the Python tool server. That is not ideal for a Python-first harness because Python no longer clearly owns process lifecycle, state, logging, and configuration.
- Tool execution is mostly synchronous and does not support streaming as cleanly as B/D.
- Duplicate tool names can silently overwrite prior registrations.
- Pending RPC requests are not always removed on timeout.
- There is no high-level harness class tying server, shim, and RPC client together.

**Audit judgment**

C's integration-test pattern should be kept, but its lifecycle ownership should not. The synthesis uses C's separate faux-provider concept while keeping Python as parent process.

### Attempt D

**Best contributions**

- Best tool authoring ergonomics: decorator registration, duplicate checking, type-hint schema inference, context injection, async tools, and update streaming.
- Strong deterministic launch posture: offline, no session, no ambient extensions/skills/templates/context files.
- Packaged TS shim is good for versioning and avoids per-tool TypeScript.
- Async TCP tool server and diagnostic commands are useful.
- Real-Pi integration-test story is strong.

**Problems**

- RPC client is synchronous/threaded, which is weaker for Python async applications.
- It does not safely auto-respond to extension UI requests.
- The production shim includes fake-provider code. Test-only provider logic should not ship in the production bridge.
- Schema inference uses `nullable: true` in places; a JSON Schema `anyOf`/`null` representation is more portable.
- Module/package naming diverges (`py_pi_harness`) from the other artifacts and from the desired conceptual name.

**Audit judgment**

D contributes the best registry and launch defaults. The synthesis keeps those and replaces the sync RPC client with B/C-style async RPC and moves fake-provider support into a separate test-only extension.

## Alternatives assessed

| Approach | Fit for user's stated goal | Assessment |
|---|---|---|
| Pure Pi RPC, no extension | Low | Good for prompting/session control, but cannot register Python tools as first-class Pi tools because RPC has no dynamic tool-registration command. |
| Full TypeScript extensions | Low | Native to Pi but violates the goal of authoring tools/extensions in Python. |
| Node/TypeScript SDK wrapper with Python API | Medium | Viable as a later sidecar if RPC lacks necessary features, but it expands the required TypeScript surface. |
| MCP adapter | Medium | Useful if tools should be shared across multiple agents, but it adds another protocol and Pi still needs an adapter extension. |
| Fork Pi to add non-TS extension API | Medium/low | Could be ideal long term, but high maintenance and not necessary for initial use. |
| Reimplement Pi in Python | Low | Maximizes Python purity but discards the core value of using Pi. |
| Python parent + Pi RPC + minimal TS bridge | High | Best balance: Python-first authoring with minimal TypeScript, using Pi's native extension/tool mechanism. |

## Recommended best practices

1. **Python owns lifecycle.**  
   Start the Python tool server first, then start Pi in RPC mode with explicit bridge extension paths.

2. **Use TypeScript only as a generic adapter.**  
   No per-tool TypeScript. The shim should fetch a manifest and dispatch executions to Python.

3. **Disable ambient resources by default.**  
   Use `--offline`, `--no-session`, `--no-extensions`, `--no-skills`, `--no-prompt-templates`, and `--no-context-files`, then explicitly pass `--extension <bridge.ts>`. Use `--no-builtin-tools` when the desired run should expose only Python tools.

4. **Do not use `--no-tools` for a Python-tool run.**  
   That can suppress custom tools. Disable built-ins with `--no-builtin-tools` instead.

5. **Implement strict byte-oriented JSONL.**  
   Split only on LF bytes. Do not use text `splitlines()` because Unicode separators may appear inside JSON strings.

6. **Auto-handle extension UI in headless mode.**  
   Default-confirm false and cancel input/select/editor requests unless the embedding app supplies handlers.

7. **Keep fake providers test-only.**  
   They are excellent for integration testing but should not be compiled into the production bridge.

8. **Use conservative schemas.**  
   Prefer explicit JSON Schema for public tools. For inferred schemas, use a conservative subset and pass it through `Type.Unsafe` in the shim.

9. **Support streaming and cancellation.**  
   Python tools should be able to emit updates. The TS bridge should close/destroy the socket on Pi `AbortSignal`; Python should expose cooperative cancellation through `ToolContext.cancelled`.

10. **Treat all tools/extensions as trusted code.**  
    Pi extensions and Python tools run with process-level access. The bridge token protects against accidental local access, not against malicious code already running as the user.

11. **Test at three layers.**  
    Use pure Python unit tests, fake-RPC subprocess tests, and real-Pi/faux-provider integration tests.

12. **Keep licensing simple.**  
    The synthesized Python package has no runtime dependencies and is MIT-licensed. Before workplace use, separately audit transitive npm dependencies of the Pi package/version you deploy.

## What the synthesis changed

The synthesized implementation adopts:

- B's async RPC model and automatic extension UI defaults;
- D's ergonomic registry, deterministic launch flags, async tool server, and diagnostic commands;
- C's separate faux-provider integration test concept;
- A's explicit `ToolResult`/context/update abstractions and clear architecture.

It rejects:

- A's HTTP/ThreadingHTTPServer bridge;
- B's less ergonomic explicit-args-only registry;
- C's TypeScript-spawns-Python lifecycle inversion;
- D's sync/threaded RPC client and production fake-provider code;
- any claim that this environment cannot run Pi, because the published Pi package was run and tested successfully.

## Residual risks and open work

1. **Version drift.**  
   The implementation was tested against Pi `0.74.0`. Pin Pi versions and run integration tests on upgrades.

2. **Schema compatibility.**  
   `Type.Unsafe` accepts JSON schema-like objects, but the bridge should keep contract tests around every exposed schema.

3. **Cancellation is cooperative.**  
   A killed socket can mark `ToolContext.cancelled`; a CPU-bound Python function must check that flag or run in a worker with stronger interruption controls.

4. **Security isolation is minimal.**  
   For untrusted tools, add OS-level sandboxing or containerization. The bridge token is not a sandbox.

5. **Production observability.**  
   Add structured logs, metrics, and trace IDs before using the harness for long-running automation.

6. **Tool state management.**  
   Stateful tools should either use sequential execution or explicit locks.
