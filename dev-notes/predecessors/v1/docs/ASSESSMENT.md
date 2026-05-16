# Assessment: Python-first Pi harness strategies

## Primary recommendation

Adopt the RPC-plus-static-shim architecture.

The Python process should own orchestration, tool registration, local resources, logging, and test harnesses. Pi should be launched as a subprocess in `--mode rpc`. A single static TypeScript extension should be loaded with `--extension`; it asks the Python process for a tool manifest, calls `pi.registerTool()` for every Python tool, and forwards tool executions back to the Python server.

This maps cleanly onto Pi's architecture:

- RPC mode is explicitly intended for non-Node embedding.
- Extension tools are the native way for Pi to add LLM-callable tools.
- Extension factory functions can be asynchronous, so tool discovery can happen at startup.
- Tool execution callbacks are small enough to bridge over IPC.
- Pi's agent loop already emits events for tool start, update, end, message streaming, and turn lifecycle.

## Alternative approaches

### 1. Direct TypeScript SDK integration

**Shape:** Write a TypeScript/Node application around `createAgentSession()`, `customTools`, and `DefaultResourceLoader`.

**Pros:** Maximum fidelity. Full SDK lifecycle control. No subprocess boundary. Best access to session manager, resources, model registry, and extension event bus.

**Cons:** Violates the goal of Python-exclusive customization. Python tools would still need a bridge, and the TS side would become a real application rather than a minimal shim.

**Verdict:** Technically strongest, strategically wrong for this user goal.

### 2. RPC mode only, no extension shim

**Shape:** Python sends prompts over RPC and observes events; tools are only built-ins or CLI/skills documented for the model.

**Pros:** Minimal. No custom TS at all.

**Cons:** Does not provide Python-defined LLM-callable tools. The model can invoke Python indirectly through bash/CLI instructions, but Pi does not see these as first-class tools with schemas, tool events, or structured results.

**Verdict:** Good for simple automation, inadequate for a Python-authored harness with real tools.

### 3. Fork Pi and add a native Python extension bridge

**Shape:** Modify Pi so extensions can be registered over a generic protocol without TypeScript.

**Pros:** Cleanest long-term user experience. Could support Python, Rust, Go, or any language.

**Cons:** High maintenance. Requires tracking Pi internal changes. Adds risk to provider/tool/session internals.

**Verdict:** Worth proposing upstream after validating the bridge design, but not the right first implementation.

### 4. MCP server in Python plus a Pi MCP extension

**Shape:** Implement tools as an MCP server and load a TypeScript extension that adapts MCP tools to Pi tools.

**Pros:** Reuses an ecosystem protocol. Useful if the same tools must serve multiple clients.

**Cons:** More moving parts than a purpose-built bridge. Pi intentionally does not ship MCP as a core primitive. MCP is tool-centric but does not automatically expose Pi-specific session/UI/event semantics.

**Verdict:** Reasonable for multi-client tool ecosystems; not ideal for a Python-first Pi harness library.

### 5. Reimplement Pi's harness in Python

**Shape:** Treat Pi as design inspiration and recreate the agent loop, tool execution, sessions, provider handling, and compaction in Python.

**Pros:** Pure Python.

**Cons:** Discards the value of Pi as the harness. Reimplements complex behavior already present in Pi, including streaming message assembly, tool execution ordering, provider abstraction, sessions, and extension events.

**Verdict:** Not recommended.

### 6. TypeScript SDK sidecar service

**Shape:** Build a Node service around `createAgentSession()` and expose a Python API over JSON-RPC/WebSocket/gRPC.

**Pros:** More control than CLI RPC; can expose SDK-only surfaces. Keeps Python as the user-facing language.

**Cons:** Larger TypeScript surface than the static shim. You must design, version, and maintain a second full RPC API over Pi's SDK.

**Verdict:** Best second-stage architecture once the lightweight shim hits real limitations.

## Decision matrix

| Approach | Python authoring | Pi fidelity | TS maintenance | Time-to-working | Recommended |
|---|---:|---:|---:|---:|---:|
| RPC + static TS shim | High | High | Low | High | Yes |
| Direct TS SDK | Low | Very high | High | Medium | No for this goal |
| RPC only | High | Medium | None | High | No, lacks tools |
| Pi fork | High | Very high | Very high | Low | No initially |
| MCP adapter | High | Medium-high | Medium | Medium | Situational |
| SDK sidecar | High | Very high | Medium-high | Medium | Future option |
