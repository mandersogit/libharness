# Implementation Journal

## Source inspection

The uploaded Pi source tree is a monorepo containing, among others:

- `@earendil-works/pi-coding-agent`
- `@earendil-works/pi-agent-core`
- `@earendil-works/pi-ai`
- `@earendil-works/pi-tui`
- `@earendil-works/pi-web-ui`

Key source findings:

- `packages/coding-agent/src/modes/rpc/rpc-mode.ts` implements RPC mode over strict JSONL.
- `packages/coding-agent/src/modes/rpc/jsonl.ts` uses LF-only framing and strips a trailing CR.
- `packages/coding-agent/src/modes/rpc/rpc-types.ts` defines `prompt`, `get_state`, `set_model`, `get_available_models`, `get_commands`, session commands, bash commands, and extension UI request frames.
- `packages/coding-agent/src/core/extensions/types.ts` defines `ExtensionAPI`, `registerTool`, `registerCommand`, `registerProvider`, event hooks, and command contexts.
- `packages/coding-agent/src/core/extensions/loader.ts` loads TypeScript extensions with `jiti` and aliases Pi packages for extension imports.
- `packages/coding-agent/src/core/tools/tool-definition-wrapper.ts` adapts extension `ToolDefinition` objects into core agent tools.
- `packages/coding-agent/src/core/agent-session.ts` merges built-ins, extension tools, and SDK custom tools into the active tool registry.
- `packages/agent/src/harness/agent-harness.ts` provides a lower-level harness, but using it directly from Python would require a larger TypeScript sidecar.

## Build attempt

A full root `npm install` initially failed because the web-ui workspace attempted to fetch a SheetJS package from a CDN. I avoided that by installing only the relevant workspaces and root dev tooling.

The normal `npm run build` for `@earendil-works/pi-ai` tried to regenerate model metadata via network calls. In this environment, those calls failed. I restored the checked-in generated model files and manually built with `tsgo`:

```bash
npx tsgo -p packages/ai/tsconfig.build.json
npx tsgo -p packages/agent/tsconfig.build.json
npx tsgo -p packages/tui/tsconfig.build.json
npx tsgo -p packages/coding-agent/tsconfig.build.json
npm run copy-assets -w @earendil-works/pi-coding-agent
chmod +x packages/coding-agent/dist/cli.js
```

## Pi RPC smoke test

Command:

```bash
printf '{"id":"1","type":"get_state"}\n' \
  | PI_OFFLINE=1 node packages/coding-agent/dist/cli.js \
      --mode rpc --no-session --no-extensions --no-skills \
      --no-prompt-templates --no-context-files \
      --provider google --model google/gemini-2.5-pro
```

Result: Pi returned a successful `get_state` response. No LLM call was required.

## Python package implementation

Created package: `py-pi-harness`.

Files:

- `src/py_pi_harness/tools.py`: registry, schema inference, context injection, result normalization.
- `src/py_pi_harness/server.py`: local JSONL tool server.
- `src/py_pi_harness/client.py`: strict JSONL Pi RPC subprocess client.
- `src/py_pi_harness/harness.py`: starts tool server and Pi subprocess together.
- `src/py_pi_harness/shims/python_tools_extension.ts`: static TypeScript adapter loaded by Pi.
- `tests/test_unit.py`: Python tests.
- `examples/basic.py`: basic usage example.

## Tests run

### Unit tests

```bash
cd /mnt/data/pi_python_harness
python3 -m pytest -q
```

Result:

```text
3 passed in 0.17s
```

### Pi shim registration test

Started Pi from the built source checkout with the Python shim. Confirmed:

- `get_state` returned the selected model;
- `get_commands` included `/py-tools` and `/py-tool`;
- `/py-tools` returned `Python tools: echo, add` as an RPC extension UI notification;
- `/py-tool echo {"text":"hi"}` returned `echo:hi`.

### Fake provider tool-loop test

Started Pi with `PY_PI_FAKE_PROVIDER=1`, registered the fake provider, switched to `py-pi-fake/toolcaller`, and prompted Pi. Observed the following event path:

- `agent_start`
- first `turn_start`
- assistant tool-call streaming events
- `tool_execution_start`
- `tool_execution_end`
- tool-result message insertion
- second `turn_start`
- final assistant text streaming events
- `turn_end`
- `agent_end`

Final assistant text:

```text
Fake provider observed Python tool result for echo:
echo:sample
```

## Known limitations of the prototype

- Not packaged/published to PyPI.
- Does not include robust provider-specific schema normalization.
- Does not yet expose every Pi RPC command as a typed method.
- Does not yet convert all Pi events into typed Python dataclasses.
- Cancellation currently aborts the bridge call; Python function-level cooperative cancellation needs a richer context.
