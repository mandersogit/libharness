# Executive Summary

## Recommendation

Use Pi in RPC mode as the Python-owned outer process, and load one minimal TypeScript extension shim that registers Python-defined tools and forwards tool execution over a local JSONL bridge.

This is the best near-term architecture for a Python-exclusive customization workflow because it:

- keeps Pi unmodified;
- keeps all user-authored tools in Python;
- preserves Pi's existing session, provider, tool-call, and event loop behavior;
- uses Pi's documented RPC embedding mode rather than scraping terminal output;
- avoids committing to a larger TypeScript SDK sidecar until deeper SDK control is needed.

The required TypeScript should be treated as infrastructure, not user customization. The shim is static, small, versioned, and generated/owned by the Python package.

## What was built

The prototype package `py-pi-harness` contains:

- a Python `ToolRegistry` with decorator-based tool authoring;
- JSON-schema inference from Python function signatures;
- a Python JSONL tool server;
- a strict JSONL `PiRpcClient` for `pi --mode rpc`;
- a packaged TypeScript extension shim that registers Python tools with `pi.registerTool()`;
- diagnostic slash commands `/py-tools` and `/py-tool`;
- an optional shim-provided fake provider for no-LLM tool-loop testing;
- unit tests and source-checkout integration tests.

## Test status

Completed in this environment:

1. Installed and built the relevant Pi workspaces from the uploaded source tree while avoiding the web-ui dependency that required a CDN fetch.
2. Confirmed the built Pi CLI starts in RPC mode and responds to `get_state` without an LLM call.
3. Ran Python unit tests for registry, schema inference, server manifest, server execution, and streaming updates.
4. Launched Pi with the Python shim, confirmed the Python tools appeared through `/py-tools`, and executed a Python tool through `/py-tool`.
5. Registered the fake provider, switched Pi to it through RPC, prompted Pi, observed Pi emit a tool call, execute the Python tool, then produce a final assistant message using the tool result.

No external LLM call was made.

## Main caveats

- Pi's native extension API is TypeScript-first. A shim is unavoidable unless Pi adds a native non-TS extension bridge.
- RPC mode exposes many but not all interactive/TUI surfaces. For deep UI integration, a TS SDK sidecar or upstream Pi changes would be better.
- Tool schemas need careful normalization for provider-specific quirks. The prototype emits ordinary JSON Schema through TypeBox `Type.Unsafe`; this worked in the tested path, but production should add schema compatibility tests for the target providers.
- Tool bridge authentication is a random localhost token. Production should additionally enforce localhost binding, process ownership assumptions, and possibly Unix-domain sockets where supported.
