# Assessment of approaches

## Recommended approach

**Python host + Pi RPC mode + generic TypeScript tool shim + Python tool broker.**

This is the best balance between Python-first customization and preserving Pi as the core harness.

### Why this works

Pi cleanly separates:

- process-level control through RPC mode;
- tool registration and extension hooks through TypeScript extensions;
- agent-loop/session/tool execution internals inside the Pi packages.

The important source-code constraint is that RPC does not contain a dynamic `register_tool` command. Tool registration happens through extensions or the TypeScript SDK. Therefore, a Python-only host can control Pi over RPC, but it cannot make Python tools visible to the LLM unless a Pi extension registers those tools. A generated generic TS extension is the smallest stable adapter.

### Recommended architecture in one line

Python writes a manifest of tool schemas, starts a local token-protected broker, launches `pi --mode rpc --no-extensions --extension <generated-shim.ts>`, and the shim registers tools whose `execute()` methods POST to Python.

## Alternatives

| Approach | Fit for Python-first goal | Strengths | Weaknesses | Recommendation |
|---|---:|---|---|---|
| Pure Python host using Pi RPC only | Medium | Very small integration; no TS shim; good for prompts/events/session control | No native custom Python tools exposed to Pi's LLM tool registry | Use only if you do not need custom tools |
| Python host + RPC + generic TS shim + Python broker | High | Keeps tools, state, orchestration, and environment in Python; preserves Pi harness; TS is static/generic | Extra process/HTTP boundary; must maintain shim compatibility with Pi extension API | Recommended |
| Node/TypeScript SDK embedding + Python sidecar | Low/medium | Best native control over Pi; no subprocess RPC; direct customTools | Host becomes TS-first; Python tools still need bridging | Use only for a TS product |
| Full TypeScript Pi extensions | Low | Most native Pi extension experience; direct event/tool/UI access | Violates Python-first constraint | Not recommended for this user preference |
| Python tools invoked by TS extension as per-call subprocesses | Medium | Simple shim; no long-running broker | Slow for frequent tools; hard to share in-memory Python state; weaker cancellation/progress | Acceptable for rare, slow tools; inferior as a general harness |
| MCP server in Python + TS MCP extension | Medium | Interoperable protocol; reusable tools outside Pi | Pi does not ship MCP as the core path; adds protocol/dependency surface; more TS than needed | Consider later if interoperability matters more than minimality |
| Fork Pi to add native Python plugin registration | High in theory | Could remove shim eventually | Expensive maintenance burden; risky with upstream changes | Not the first move |
| Rebuild the harness in Python with another framework | High Python purity | Maximum Python control | Loses Pi's session model, tool loop, provider integrations, and extension ecosystem | Only if Pi itself becomes a poor fit |

## Key design judgment

Do not write one TypeScript extension per Python tool. Generate one generic extension that reads a manifest and forwards execution. That preserves Python as the authoring surface and makes the TypeScript footprint stable.

## Operational notes

- Use `--extension <generated-shim.ts>` to inject the shim.
- Use `--no-extensions` by default to avoid accidentally loading unrelated local/global extensions. CLI-specified extensions still load.
- Use `--no-builtin-tools` if you want only Python tools. Avoid `--no-tools` unless you explicitly reactivate tools, because it disables built-in and extension/custom tools by default.
- Use `--tools read,bash,my_python_tool` when you need an allowlist.
- Keep the broker bound to `127.0.0.1` with a random token.
- For mutating tools, either set `executionMode: "sequential"` or implement Python-side file locks/queues. Pi runs tool calls in parallel by default unless a tool opts out.
