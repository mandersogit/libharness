# Executive summary

## Recommendation

Use the **Python parent + Pi RPC mode + generated TypeScript tool shim** architecture.

This is the best fit for a Python-first customization workflow because it uses Pi's supported integration surfaces without requiring you to author normal extensions in TypeScript. Python owns the environment, process launch, tool definitions, schemas, policy, and higher-level orchestration. The remaining TypeScript is a small generated adapter that registers tools with Pi and forwards calls to Python.

## Why this approach wins

Pi's core customization surface is TypeScript extensions. Pi's non-TUI embedding surface is RPC over stdin/stdout. Combining those two gives a practical split:

- Pi remains the agent runtime and coding harness.
- Python remains the customization language.
- The TypeScript layer is narrow, deterministic, and replaceable.
- You avoid forking or reimplementing Pi's tool-calling, session, compaction, provider, and event machinery.

## Primary caveat

The approach can make **tools** Python-native immediately. It does not automatically make every Pi extension capability Python-native. Event interception, commands, provider registration, custom TUI rendering, and advanced session mutations would need additional bridge messages and a somewhat larger generated shim.

## Prototype included

The artifact contains a Python package named `pi-python-harness` with:

- `PiRpcClient`: async Python client for `pi --mode rpc`.
- `ToolRegistry`: decorator-based Python tool registration.
- `PythonToolServer`: loopback JSONL server for Python tool calls.
- generated TypeScript shim source in `shim.py`.
- `PythonPiHarness`: high-level launcher that writes the manifest, starts the tool server, and launches Pi with the shim.
- tests for JSONL framing, tool bridging, shim generation, and fake-RPC subprocess behavior.

Local test result:

```text
7 passed in 0.32s
```

## What I could not run here

I could not build or run the uploaded Pi checkout in this container. The uploaded Pi packages require Node 20+ (`packages/coding-agent/package.json` requires `node >=20.6.0`; the root package requires `node >=20.0.0`), while the environment has Node 18.20.4. Dependency installation emitted Node engine warnings and did not complete successfully. I therefore validated the Python package with unit tests and a fake RPC process rather than a live Pi process.

## Artifact map

- `README.md`: package overview and minimal usage example.
- `docs/assessment.md`: approach comparison and recommendation.
- `docs/design-rpc-python-tool-bridge.md`: detailed architecture and protocol design.
- `docs/source-findings.md`: source evidence from the uploaded Pi codebase.
- `docs/implementation-journal.md`: what was inspected, attempted, implemented, and tested.
- `docs/runbook.md`: setup and first-run steps for a machine with Node 20+ and Pi installed.

## Next engineering steps

1. Run this package against a real Pi install on a Node 20+ machine.
2. Expand the bridge from tool-only to event interception if your harness needs permission gates, context injection, or policy enforcement before/after Pi tool calls.
3. Add per-tool cancellation propagation from the shim into Python, instead of using socket close as the only cancellation signal.
4. Decide whether mutating Python tools should default to `executionMode: "sequential"` or use explicit file/resource locks.
5. Add a packaging mode that writes a pinned shim file and manifest to a project-local `.pi` directory when you want reproducible runs rather than temporary generated files.
