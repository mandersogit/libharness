# Run results

## Environment notes

- Date: 2026-05-14.
- Uploaded Pi source extracted to `/mnt/data/pi_src/pi-main`.
- Runnable Pi package installed separately under `/mnt/data/pi_npm_install`.
- Initial Python package/artifacts created under `/mnt/data/pi_python_harness_artifacts`.

## Uploaded source build attempt

The root install/build path for the uploaded source was not completed. Dependency installation attempted to fetch from an external SheetJS CDN and failed with DNS resolution in the sandbox. Because of that, I used the published Pi package matching the uploaded coding-agent package version for execution tests.

## Runnable Pi install

Command pattern used:

```bash
mkdir -p /mnt/data/pi_npm_install
cd /mnt/data/pi_npm_install
npm init -y
npm install @earendil-works/pi-coding-agent@0.74.0 --ignore-scripts --loglevel=error
node node_modules/@earendil-works/pi-coding-agent/dist/cli.js --version
```

Observed CLI version:

```text
0.74.0
```

## Real Pi RPC smoke test

Command pattern:

```bash
node /mnt/data/pi_npm_install/node_modules/@earendil-works/pi-coding-agent/dist/cli.js \
  --mode rpc \
  --no-session \
  --offline \
  --no-extensions
```

JSONL request:

```json
{"id":"1","type":"get_state"}
```

Outcome:

- Pi returned a valid `type: "response"` message.
- `command` was `get_state`.
- `success` was `true`.
- The returned state included `sessionId` and `isStreaming: false`.

## Python unit tests

Command:

```bash
cd /mnt/data/pi_python_harness_artifacts
python -m unittest discover -s tests -v
```

Observed result without `PI_CLI` or `PI_COMMAND`:

```text
test_get_state_from_real_pi_rpc_mode ... skipped 'set PI_CLI or PI_COMMAND to run real Pi integration tests'
test_python_tool_bridge_loaded_and_executed_by_faux_provider ... skipped 'set PI_CLI or PI_COMMAND to run real Pi integration tests'
test_handle_request_lists_tools ... ok
test_jsonl_server_uses_lf_only_records ... ok
test_registers_schema_from_type_hints_and_calls_tool ... ok

Ran 5 tests
OK (skipped=2)
```

## Real Pi integration tests

Command:

```bash
cd /mnt/data/pi_python_harness_artifacts
PI_CLI=/mnt/data/pi_npm_install/node_modules/@earendil-works/pi-coding-agent/dist/cli.js \
  python -m unittest tests.test_rpc_real_pi -v
```

Observed result:

```text
test_get_state_from_real_pi_rpc_mode ... ok
test_python_tool_bridge_loaded_and_executed_by_faux_provider ... ok

Ran 2 tests in 6.391s
OK
```

## What the bridge integration test proves

The integration test proves all of the following in the local environment:

1. Python can launch Pi in RPC mode.
2. Pi can load a generated TypeScript extension from a temporary path.
3. The generated extension can start a Python tool server.
4. The extension can list Python-authored tool metadata.
5. The extension can register a Python tool with Pi.
6. Pi's faux provider can emit a tool call for that tool.
7. Pi's agent loop can execute the registered tool.
8. The generated extension can forward the tool call into Python.
9. Python can return a Pi-compatible tool result.
10. Pi can emit the normal tool execution events over RPC.
