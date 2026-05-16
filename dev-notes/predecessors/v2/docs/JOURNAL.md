# Work journal

## 1. Source extraction and initial inspection

- Extracted `/mnt/data/pi-main(1).zip` to `/mnt/data/pi_src/pi-main`.
- Identified Pi as a TypeScript monorepo with packages including `coding-agent`, `agent-core`, `agent`, and `ai`.
- Found the coding-agent package version in the uploaded source as `0.74.0`.

## 2. RPC protocol inspection

Inspected:

- `packages/coding-agent/docs/rpc.md`
- `packages/coding-agent/src/modes/rpc/rpc-types.ts`
- `packages/coding-agent/src/modes/rpc/rpc-mode.ts`
- `packages/coding-agent/src/modes/rpc/jsonl.ts`

Findings:

- RPC is a real headless mode over stdin/stdout JSONL.
- RPC supports prompts, steering, follow-ups, abort, state, model changes, queue settings, compaction, bash, sessions, messages, and slash-command listing.
- RPC emits agent events asynchronously.
- RPC includes an extension UI request/response subprotocol.
- RPC does not include native dynamic tool registration.
- Strict LF-only JSONL framing is required.

## 3. Extension and tool inspection

Inspected:

- `packages/coding-agent/src/core/extensions/types.ts`
- `packages/coding-agent/src/core/extensions/loader.ts`
- `packages/coding-agent/docs/extensions.md`
- `packages/coding-agent/src/core/agent-session.ts`
- `packages/agent/src/agent-loop.ts`

Findings:

- `pi.registerTool()` is the core extension API for LLM-callable tools.
- Tools have parameter schemas, prompt additions, execution modes, and async execution functions.
- Extensions are TypeScript/JavaScript modules loaded through Pi's extension loader.
- Custom and extension tools are incorporated into the agent session runtime.
- The agent loop handles validation, execution start/update/end events, result messages, and errors once a tool is registered.

## 4. Schema validation inspection

Inspected:

- `packages/ai/src/utils/validation.ts`

Finding:

- Pi's validator can operate on plain JSON Schema-like objects, not only TypeBox metadata-bearing schemas. This allows Python to emit JSON Schema for tool parameters.

## 5. External documentation check

Checked public Pi documentation and release pages current to 2026-05-14. These confirmed the current Pi positioning, modes, RPC documentation, SDK documentation, extension model, and the move to the `@earendil-works` package scope.

## 6. Attempted source install/build

- Tried root dependency installation from the uploaded source.
- The install path failed when npm attempted to fetch a dependency from an external SheetJS CDN and DNS resolution failed in the sandbox.
- Because of that, I did not build the uploaded source tree from root.

## 7. Installed runnable Pi package

- Created a separate npm install directory at `/mnt/data/pi_npm_install`.
- Installed `@earendil-works/pi-coding-agent@0.74.0` with npm.
- Verified the CLI version output as `0.74.0`.

## 8. Ran Pi RPC mode

Started Pi with:

```bash
node /mnt/data/pi_npm_install/node_modules/@earendil-works/pi-coding-agent/dist/cli.js --mode rpc --no-session --offline --no-extensions
```

Sent `get_state` JSONL. Pi returned a valid RPC response with `isStreaming: false`.

## 9. Implemented Python package

Created `/mnt/data/pi_python_harness_artifacts` with:

- `pi_python_harness/rpc.py`
- `pi_python_harness/tools.py`
- `pi_python_harness/shim.py`
- `pi_python_harness/cli.py`
- `examples/echo_tools.py`
- `examples/launch_with_tools.py`
- `tests/test_tools.py`
- `tests/test_rpc_real_pi.py`
- `pyproject.toml`
- documentation under `docs/`

## 10. Tested package

- Ran compile checks with `python -m compileall`.
- Ran unit tests without Pi environment variables; real Pi tests were skipped as designed.
- Ran integration tests with `PI_CLI` pointed to the installed Pi CLI.
- Confirmed Pi could load the generated TypeScript shim and execute a Python tool through the bridge using a faux provider.

## 11. Final artifact packaging

Packaged the library, examples, tests, and docs into a zip archive for handoff.
