# libharness.pi — design

**Scope:** the `libharness.pi` subpackage. Other harnesses (when they
exist) get their own sibling subpackage and their own design doc.

## Goals

- Author tools, environment customization, and orchestration in Python.
- Pi remains the agent runtime — model/provider selection, session state, prompt expansion, tool registry, validation, event stream, execution loop, compaction, retry, OAuth.
- The TypeScript surface is one generic adapter that is generated / vendored once, contains no per-tool logic, and is versioned via a manifest handshake.
- Deterministic tests at three layers: pure unit, fake-pi subprocess, real-pi with a faux provider — plus an opt-in real-LLM smoke test.

## Non-goals

- Reimplementing pi or any subset of it in Python.
- Replacing pi's provider/model registry, session manager, or TUI.
- Wrapping the pi TypeScript SDK as a sidecar — RPC is the boundary.
- A production sandbox. The bridge token guards against accidental local access; it is not isolation.

## Architecture

```text
Python application
  ├─ Agent                  (subclassable; AgentHookSurface mixin with 112 ClassVars)
  │   └─ on_/async_on_/decide_/async_decide_ <event>  (37/37/19/19 hook slots)
  └─ PiAgentHarness         (thread-owned proxy)
       ├─ HarnessRuntime    (one shared asyncio loop thread + tool executor + hook executor)
       ├─ ToolRegistry      (@register decorators, JSON Schema from hints)
       ├─ PythonToolServer  (asyncio JSONL server on 127.0.0.1, token-protected)
       └─ PiRpcClient       (subprocess: pi --mode rpc, LF-only JSONL)
                                 │
                                 ▼
                            Pi process (sandboxed)
                                 └─ generic TS extension (loaded via --extension)
                                      ├─ handshake: bridge.manifest()
                                      ├─ pi.registerTool() per manifest entry
                                      ├─ execute(): forward to Python bridge
                                      └─ pi.on(eventName, …) for 19 decision events
                                          ├─ gate closed → bridgeNotify (fire-and-forget)
                                          └─ gate open   → bridgeCall   (sync round-trip)
```

The two protocols are deliberately separate:

1. **Pi RPC** between the Python parent and pi (JSONL on pi's stdin/stdout). Owned by pi; we are a client.
1. **Bridge** between the TS shim (running inside pi) and the Python tool server (loopback TCP JSONL with a bearer token). Owned by us on both sides.

Keeping them separate means pi's RPC contract can evolve without touching the bridge and vice versa.

**Threading model.** One asyncio loop thread (shared across harnesses by default; created by `HarnessRuntime`) owns subprocess and socket I/O. Each `PiAgentHarness` has its own owner thread for harness state; the public API marshals work onto that owner thread and blocks for the result. A shared tool executor runs Python tool bodies; a dedicated single-worker hook executor runs sync notification and decision hooks. The application main thread stays the application's.

**Agent class.** Optional subclassable surface on top of `PiAgentHarness`. Users override `on_<event>` / `async_on_<event>` for notification (37 events: 18 RPC notification + 19 decision-event observation slots) and `decide_<event>` / `async_decide_<event>` for decision participation (19 events). Method existence opens the corresponding bridge gate at extension load. Per-event return shapes documented in [docs/AGENT_HOOKS.md](AGENT_HOOKS.md).

## Process lifecycle

1. Python builds a `ToolRegistry` and decorates Python functions.
1. `PiPythonHarness.start()` launches `PythonToolServer` on `127.0.0.1:<ephemeral>` with a random per-run bearer token.
1. The harness writes a generated TS shim into a temp dir.
1. The harness launches pi with deterministic flags
   (`--mode rpc --offline --no-session --no-extensions --no-skills --no-prompt-templates --no-context-files --extension <shim>.ts`)
   and these env vars:
   - `PI_PY_TOOLS_HOST`, `PI_PY_TOOLS_PORT`, `PI_PY_TOOLS_TOKEN`
   - `PI_PY_BRIDGE_TIMEOUT_MS`
   - `PI_PY_DIAGNOSTIC_COMMANDS`
   - `HOME` is pinned at the sandbox path so pi's `~/.pi/` lands
     inside `.sandbox/pi-home/.pi/`.
1. Pi loads the shim via `jiti`. The shim issues a `manifest` bridge call, asserts `protocolVersion == 1`, and calls `pi.registerTool()` for each tool.
1. Python sends prompts and control commands via `PiRpcClient`.
1. When the model emits a tool call, pi validates the args, calls the shim's `execute()`, which forwards an `execute` bridge call to Python. Python runs the tool, optionally streams `update` frames, and returns a final `response` frame. The shim normalizes to pi's `AgentToolResult`.

## Bridge protocol

LF-only JSONL on loopback TCP. One JSON object per connection request, streaming responses (zero or more `update` frames, then exactly one `response`).

### Handshake

```json
{"id":"m1","type":"manifest","token":"..."}
```

```json
{
  "id": "m1",
  "type": "response",
  "success": true,
  "data": {
    "protocolVersion": 1,
    "tools": [
      {
        "name": "echo",
        "label": "echo",
        "description": "Echo a message",
        "parameters": {"type": "object", "properties": {...}, "required": [...], "additionalProperties": false}
      }
    ]
  }
}
```

The shim throws if `protocolVersion` does not equal its compiled-in constant. Mismatches fail extension load loudly rather than producing confused runtime errors.

### Execute

```json
{
  "id": "e1",
  "type": "execute",
  "token": "...",
  "tool": "echo",
  "toolCallId": "pi-tool-call-id",
  "params": {"message": "hi"},
  "cwd": "/workspace",
  "context": {"hasUI": true, "model": {"provider":"openai-codex","id":"gpt-5.5"}}
}
```

Streaming update (zero or more before the final response):

```json
{"id":"e1","type":"update","data":{"content":[{"type":"text","text":"working..."}],"details":{}}}
```

Final response:

```json
{"id":"e1","type":"response","success":true,"data":{"content":[{"type":"text","text":"3"}],"details":{}}}
```

Error response:

```json
{
  "id": "e1",
  "type": "response",
  "success": false,
  "error": "ValueError: ...",
  "data": {"details": {"exceptionType": "ValueError", "traceback": "..."}}
}
```

The shim raises on `success: false`. Pi records a tool failure in its agent loop.

## Python API

### `ToolRegistry`

```python
registry = ToolRegistry()

@registry.register(description="Add two integers")
def add(a: int, b: int) -> ToolResult:
    return ToolResult.text(str(a + b), details={"a": a, "b": b})

@registry.register()
async def echo(message: str, ctx: ToolContext) -> ToolResult:
    await ctx.update(f"received: {message}")
    return ToolResult.text(f"echo: {message}")
```

Schema inference covers `str / int / float / bool / list[T] / dict[K, V] / Optional[T] / Union[…] / Literal[...] / Enum / @dataclass`. Pass an explicit `parameters=` dict for anything outside that subset. Tool names are validated against a conservative regex; duplicates raise `ToolError`.

A parameter named `ctx` or annotated as `ToolContext` is treated as the execution context and excluded from the JSON Schema.

Tool handlers may be sync or async, may return `ToolResult` / `str` / `Mapping` / `@dataclass` / scalar (`normalize_tool_value` handles each), and may be sync- or async-generators yielding intermediate results — yielded values become `update` frames; the last value becomes the final response.

### `ToolContext`

```python
ctx.tool_call_id        # opaque id from pi
ctx.tool_name           # tool name pi invoked
ctx.cwd                 # pi's working directory
ctx.metadata            # context dict forwarded by the shim
ctx.cancelled           # set when pi aborts via AbortSignal
await ctx.update(value) # stream an update frame to pi
```

Cancellation is cooperative: when pi aborts the tool call, the TS shim destroys the bridge socket, which the server-side `watch_disconnect` task observes and uses to set `ctx.cancelled`. Long-running Python tools must check `ctx.cancelled` themselves.

### `PiRpcClient`

Async client over `pi --mode rpc`. Strict LF-only JSONL framing. Request/response correlation by `id`; events go to an `asyncio.Queue` plus subscribed handlers. Methods:

```python
await client.prompt(message)
await client.prompt_and_wait(message)   # waits for agent_end
await client.steer(...) / follow_up(...) / abort() / new_session()
await client.get_state() / get_messages() / get_commands() / get_available_models()
await client.set_model(provider, model_id)
await client.bash(command)
await client.next_event() / wait_for_event(event_type)
await client.get_last_assistant_text()

client.on_event(handler)                # returns unsubscribe fn
client.set_extension_ui_handler(method, handler)
```

Extension UI requests from pi are handled automatically in headless mode: `select / input / editor` get `cancelled: true`, `confirm` gets `confirmed: false`, fire-and-forget methods (`notify / setStatus / setWidget / setTitle / set_editor_text`) are queued as events without a response.

### `PiPythonHarness`

```python
async with PiPythonHarness(registry, config=PiLaunchConfig(...)) as harness:
    state = await harness.client.get_state()
    await harness.client.prompt_and_wait("...")
```

Owns the broker + generated shim + `PiRpcClient` lifecycle. Can also launch a separate test-only faux-provider extension (`fake_provider=True`) that emits a configured tool call followed by a final message — used by `test_real_pi_integration.py` to exercise pi's full tool loop without an LLM.

### Sessions

We use **pi-native sessions** as the persistence and history-navigation model. Libharness does not maintain a parallel Python-side session record. Pi already represents history as a cross-file tree (each entry has a `parentId`; each session header can declare a `parentSession`), and forking from any entry in any prior session is a first-class operation. Reinventing that in Python would duplicate a non-trivial data structure for no clear gain.

Persistence is configured on `PiLaunchConfig`:

| Field         | Default | Effect                                          |
| ------------- | ------- | ----------------------------------------------- |
| `no_session`  | `True`  | ephemeral; no JSONL written to disk             |
| `session_dir` | `None`  | overrides `~/.pi/agent/sessions/<encoded-cwd>/` |
| `session`     | `None`  | resume a session by path or partial UUID prefix |

Currently exposed as typed methods on `PiRpcClient`: `new_session()`, `get_messages()`, `get_last_assistant_text()`. The rest of pi's session API (`fork`, `clone`, `switch_session`, `get_session_stats`, `export_html`, `set_session_name`, `get_fork_messages`) is reachable today via `client.send({"type": "..."})` and is planned to grow typed wrappers — see SESSION-STATE.md for the pending task.

Background on pi's session model — the tree structure, the per-file vs per-session ID rules, and how forking actually writes new files — is in `dev-notes/2026-05-14-pi-internals-notes.md`.

## TypeScript shim

A single ~250-line file (`PRODUCTION_TS_SHIM` in `src/libharness/pi/shim.py`). Generated to a temp file at startup. No per-tool logic. Imports only `node:net`, `node:crypto`, `typebox`, and two types from `@earendil-works/pi-coding-agent`.

Responsibilities:

1. Read bridge coordinates from env.
1. Issue `manifest` bridge call; assert `protocolVersion`.
1. Register each tool with `pi.registerTool()`, wrapping the JSON-Schema params via `Type.Unsafe(...)`.
1. On `execute`, forward to the bridge and normalize the response into pi's `AgentToolResult` shape.
1. Forward Pi's `AbortSignal` into the bridge call (socket destroy).
1. Optionally register diagnostic slash commands `/py-tools` and `/py-tool` (gated on `PI_PY_DIAGNOSTIC_COMMANDS`).

The faux-provider extension is a separate generated file written only when `fake_provider=True`. It uses pi-ai's real `registerFauxProvider`, `fauxAssistantMessage`, and `fauxToolCall` APIs.

## Failure modes

| Failure                      | Expected behavior              | Mitigation                            |
| ---------------------------- | ------------------------------ | ------------------------------------- |
| Pi process fails to start    | `PiRpcProcessError` + stderr   | Validate `PI_CLI` before launch       |
| Invalid manifest             | Shim throws on extension load  | Pin `MANIFEST_PROTOCOL_VERSION`       |
| Python tool raises           | Bridge `success: false`        | App-level error policy + obs.         |
| Bridge unreachable           | Shim connection error          | Server starts before pi               |
| RPC JSON parse error         | `StrictJsonlDecoder` raises    | LF-only framing; 16 MB buffer cap     |
| Pi aborts tool call          | `ctx.cancelled` set via socket | Long-running tools check the flag     |
| Concurrent mutation conflict | Race on shared state           | `execution_mode="sequential"` or lock |
| Pi RPC field-name drift      | `PiRpcError` raised            | Per-command regression tests          |

**Detail:**

- *Pi process fails to start.* `PiRpcClient.start()` raises `PiRpcProcessError` with the captured stderr; callers should validate the `PI_CLI` path before reaching this point.
- *Invalid manifest.* The TS shim throws during extension load and pi surfaces the error. Mitigation is to pin `MANIFEST_PROTOCOL_VERSION` on both sides so drift is caught at the handshake.
- *Python tool raises.* Bridge returns `success: false`; shim raises; pi records a tool failure in the agent loop. Add an app-level error policy and observability layer.
- *Bridge unreachable.* Shim connection error; the tool call fails. The server is started before pi launches; readiness is implicit (bound socket).
- *RPC JSON parse error.* `StrictJsonlDecoder` raises with a preview of the offending bytes. Mitigations: LF-only framing on both sides; a bounded buffer (16 MB) prevents memory exhaustion on unterminated frames.
- *Pi aborts tool call.* The shim destroys the bridge socket; the server's `watch_disconnect` task sets `ctx.cancelled`. Long-running Python tools must check the flag.
- *Concurrent mutation conflict.* Race possible if two parallel tools touch shared state. Mark mutating tools `execution_mode="sequential"` or implement per-resource locks.
- *Pi RPC field-name drift.* Pi returns `success: false` and the client raises `PiRpcError`. Add per-command regression tests (cf. `tests/pi/test_set_model.py`).

## Roadmap

The current scope is **tool execution only**. v3's design called out three further customization surfaces that match pi's extension API. None of them are implemented; they all build on the same bridge.

### Event bridge

Forward pi extension events (`tool_call`, `tool_result`, `session_start`, `agent_start`, `agent_end`, `message_*`, …) into Python event handlers. Enables permission gates, path protection, context injection, logging.

Co-design required before implementation. Proposal: `dev-notes/2026-05-14-event-bridge-proposal.md`.

### Command bridge

Let Python register pi slash commands (`/something`) the same way extensions can. The shim mirrors a Python-side command manifest into `pi.registerCommand()` and forwards invocation back.

### State bridge

Let Python query and mutate session state where pi exposes it: append entries, set active tools, set model, get commands, reload. Requires careful contracts around mutation timing.

### UI bridge

RPC already exposes `extension_ui_request` / `extension_ui_response` for dialogs. The harness already auto-handles these in headless mode. A richer UI bridge would let Python implement `select / input / confirm / editor` flows interactively — useful for non-pi UIs hosting the harness. Beyond that, pi's TUI component factories are not RPC-accessible; a Python-first integration should render its own UI on top of the event stream rather than try to project pi's TUI.

## Resolved decisions

Decisions made and their rationale. New decisions append; old ones stay for historical context (mark as superseded if reversed, never delete).

| Date       | Decision                                                                                                       |
| ---------- | -------------------------------------------------------------------------------------------------------------- |
| 2026-05-14 | Bridge transport: loopback TCP + bearer token, no UDS                                                          |
| 2026-05-14 | Sessions: pi-native; no Python-side session model                                                              |
| 2026-05-15 | Concurrency: threads, freethreading-first (**Superseded** by 2026-05-17 row)                                   |
| 2026-05-17 | Concurrency: asyncio core in dedicated thread; Agent class with opt-in hooks                                   |
| 2026-05-17 | Hook surface layout: `AgentHookSurface` public mixin; 3-file split                                             |
| 2026-05-17 | Decision-hook return shape: raw dict per pi's TS event-result types                                            |
| 2026-05-17 | Cancellation API: `HookContext` with cooperative polling on `threading.Event`                                  |
| 2026-05-17 | Timeout config: `_decision_timeout_ms` (global) + `_decision_timeouts_ms` (per-event), both opt-in, no default |

**Detail:**

- *2026-05-14 — Bridge transport:* Loopback TCP + bearer token, not a
  Unix domain socket. The token already addresses the realistic
  local-process threat; UDS would be a marginal hardening, not a
  structural fix. Windows support preserved for free. Reference:
  `dev-notes/2026-05-14-pi-internals-notes.md` § Bridge transport.
- *2026-05-14 — Sessions:* Use pi-native sessions; no Python-side
  session model. Pi already maintains a cross-file tree-of-entries with
  first-class fork/branch operations. Replicating in Python would
  duplicate non-trivial state with no clear gain. Reference:
  `dev-notes/2026-05-14-pi-internals-notes.md` § Session structure.
- *2026-05-15 — Concurrency (Superseded):* Primarily **D** — threads on
  freethreaded CPython 3.14t. Implemented as far as phase 3 on the
  parallel `threads-rewrite` branch; doubts surfaced about the
  rip-asyncio-out approach (thread inventory ballooning, lock-order
  complexity, sync-callback ergonomics). Superseded 2026-05-17 by the
  asyncio-in-thread direction below. Body preserved for audit trail at
  `dev-notes/2026-05-15-concurrency-model-discussion.md` and
  `dev-notes/2026-05-15-threads-rewrite-plan.md`.
- *2026-05-17 — Concurrency:* Keep the asyncio core but run it in a
  dedicated thread (`HarnessRuntime` + `AsyncioLoopThread`). Each
  `PiAgentHarness` has its own owner thread; the public API marshals to
  it. Tool bodies run on a shared `ThreadPoolExecutor`; sync hooks on a
  dedicated single-worker hook executor. Verified on both `local.venv`
  (3.11) and `local-ft.venv` (3.14t freethreaded). Full rationale at
  `dev-notes/2026-05-17-v6-as-base-direction.md` and the design
  analysis at `dev-notes/2026-05-17-v8-analysis.md`.
- *2026-05-17 — Hook surface layout:* `AgentHookSurface` is a public
  mixin (no leading underscore) with 112 ClassVar declarations covering
  every observable hook flavor for every event. Three-file split:
  `events.py` (data types: `AgentEvent`, `HookContext`,
  `UnhandledEventError`), `hook_surface.py` (mixin + validation), and
  `agent_class.py` (the `Agent` class with dispatchers). An import-time
  consistency-check guard pins the invariant that the 112 ClassVars
  match the event frozensets.
- *2026-05-17 — Decision-hook return shape:* Raw Python `dict` matching
  pi's TypeScript `extensions/types.ts` event-result union. No
  TypedDicts in v1; per-event shapes documented in
  [docs/AGENT_HOOKS.md](AGENT_HOOKS.md). Revisit if a user-facing
  case for compile-time shape safety surfaces.
- *2026-05-17 — Cancellation API:* `HookContext` (a frozen dataclass
  with `event_name`, `request_id`, `_cancelled: threading.Event`) is
  passed as an optional second positional to decision hooks. Hooks
  opt in by including `ctx` (or `*args`); the dispatcher detects via
  `inspect.signature`. `ctx.cancelled` is a property that polls the
  Event; long-running hooks should check it and return `None`.
- *2026-05-17 — Timeout config:* Both `_decision_timeout_ms`
  (single int, applies to every open gate) and `_decision_timeouts_ms`
  (`dict[str, int]`, per-event overrides). Per-event entries beat
  global. Both default to nothing — no timeout is enforced unless a
  subclass opts in. Rationale: human-in-the-loop decision hooks may
  legitimately need to block for hours or days. A timeout default
  would assume human-not-in-the-loop, which the library does not.

## When to switch away

- **Pi SDK directly** if the surrounding product is Node/TypeScript and wants in-process access to `AgentSession` on every operation.
- **Patch/fork pi** only if Python plugin registration without a TS shim is mandatory and you'll maintain upstream compatibility.
- **Reimplement pi** — discards the value of using pi; not recommended.
