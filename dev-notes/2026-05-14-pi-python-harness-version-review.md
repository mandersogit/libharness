# Review: four parallel ChatGPT 5.5 Pro implementations of a Python-first pi harness

Reviewer: Claude Opus 4.7. Date: 2026-05-14.

Prompt the four runs were given lives at `../prompt.txt`. Each run produced
docs (assessment, design, journal, executive summary), a Python library, a
generated/embedded TypeScript shim, and a small test suite. Source for each:

- `../version-1/pi_python_harness/`
- `../version-2/pi_python_harness_artifacts/`
- `../version-3/pi-python-harness/`
- `../version-4/pi_python_harness/`

The reference pi source lives at `../pi-main/`.

## Ground truth on pi

Before reading any of the four artifacts I spot-checked the pi source so I
could score claims against reality:

- **RPC mode is real.** `--mode rpc` is wired in
  `packages/coding-agent/src/cli/args.ts` and implemented in
  `packages/coding-agent/src/modes/rpc/rpc-mode.ts` over strict LF-only JSONL.
  Commands (`prompt`, `get_state`, `set_model`, `get_messages`,
  `get_commands`, `get_last_assistant_text`, `bash`, session ops, etc.) and
  the `extension_ui_request`/`extension_ui_response` subprotocol live in
  `packages/coding-agent/src/modes/rpc/rpc-types.ts`.
- **There is no native "register a tool over RPC" command.** Tools come from
  built-ins or from extensions, which are TypeScript modules loaded via
  `jiti` and given an `ExtensionAPI` exposing `registerTool`,
  `registerProvider`, `registerCommand`, `on(...)`, etc.
  (`packages/coding-agent/src/core/extensions/runner.ts`).
- **MCP is not supported by pi.** Don't expect to integrate that way.
- **Faux provider machinery exists.** `registerFauxProvider`,
  `fauxAssistantMessage`, and `fauxToolCall` live in
  `packages/ai/src/providers/faux.ts` and are usable from extensions. This
  matters because all four versions rely on a no-LLM path for testing — the
  ones that use this API get it cheap; the ones that don't reimplement a
  fake provider by hand.
- **Schema flexibility.** Pi's tool-arg validator accepts plain JSON Schema
  objects, not only TypeBox metadata. All four versions correctly exploit
  this — they emit JSON Schema from Python type hints rather than emitting
  TypeBox TS.

The Opus suggestion described in the prompt ("launch pi in RPC mode + TS
shim") is therefore directionally correct and supported by the source.

## The architecture all four converged on

```
Python parent process
  ├─ ToolRegistry (decorator-based, JSON Schema from type hints)
  ├─ Tool bridge endpoint
  └─ PiRpcClient (subprocess: pi --mode rpc, JSONL stdin/stdout)
                      │
                      ▼
                  Pi process
                      └─ generic TS extension (loaded via --extension)
                           ├─ pi.registerTool(...) for each manifest entry
                           └─ on execute(): call back into the Python bridge
```

The variation lives in three places: (1) the tool-call bridge transport, (2)
whether the shim is packaged vs. generated, and (3) how much they actually
ran against real pi. Everything else is essentially the same.

## Side-by-side

|                         | **v1**                                                  | **v2**                                                   | **v3**                                          | **v4**                                                  |
| ----------------------- | ------------------------------------------------------- | -------------------------------------------------------- | ----------------------------------------------- | ------------------------------------------------------- |
| Tool bridge transport   | Loopback TCP JSONL + bearer token in payload            | Spawned child stdio JSONL (TS shim spawns Python server) | Loopback TCP JSONL + bearer token in payload    | Localhost HTTP + NDJSON, bearer in `X-Pi-Python-Token`  |
| PiRpcClient style       | Sync API wrapped around asyncio in a background thread  | Async-native (`asyncio.subprocess`)                      | Async-native                                    | Async-native                                            |
| TS shim                 | Pre-written, vendored in `src/.../shims/`               | Generated via string template at start                   | Generated via string template at start          | Generated via string template at start                  |
| Faux/no-LLM path        | **Hand-built** via `streamSimple` + synthetic events    | **Uses pi-ai's `registerFauxProvider`**                  | Designed only; never run end-to-end             | Designed only; never run end-to-end                     |
| Built and ran real pi?  | **Yes** — built from uploaded source, full agent loop   | **Yes** — installed `@earendil-works/pi-coding-agent` from npm | No — sandbox stuck on Node 18                   | No — sandbox stuck on Node 18                           |
| Integration tests gated by `PI_CLI` env var | Yes (2 tests pass)                  | Yes (2 tests pass)                                       | No                                              | No                                                      |
| Unit-only tests passing | 3                                                       | 3                                                        | 7                                               | 5                                                       |
| Streaming updates       | `ctx.update(...)` → bridge `update` frame               | Response shape allows `updates[]`                        | Generator yields → `tool_update` frames         | NDJSON `update` records via `ctx.emit_update`           |
| Extra niceties          | `/py-tools` and `/py-tool` slash commands for diagnostics | `python-tools` slash command                           | Manifest carries explicit `protocolVersion`     | `/healthz` and `/manifest` HTTP endpoints (easy curl)   |
| Cancellation            | Bridge socket abort; no Python-side cooperation         | Designed only                                            | Designed only (good failure-mode table though)  | Pi `AbortSignal` → `fetch` abort, no Python kill        |
| Docs                    | Solid: design + journal + testing report                | Solid: design + journal + run-results                    | **Best** docs — failure modes, versioning, future event/command/state/UI bridges | Solid: design + journal + source-evidence            |

## Highlights per version

### v1 — `py-pi-harness`

The only run that **built pi from the uploaded source tree**, hit the
`PI_OFFLINE`/`--offline` path to skip model fetching, and exercised the
full agent loop end-to-end with a faux provider, observing the real event
sequence (`agent_start` → `turn_start` → `toolcall_*` →
`tool_execution_start/end` → `turn_start` → `text_*` → `agent_end`). The
testing report quotes the observed assistant text. That's the strongest
"actually working" claim of the four.

The TS shim is **vendored, not generated**: `python_tools_extension.ts` is
shipped as a static file in the Python package and pointed at via
`--extension`. Trade-off: less flexible than generation, but easier to
audit, version, and grep.

Less attractive: the fake-provider implementation hand-pushes synthetic
events (`text_start`, `text_delta`, `text_end`, `toolcall_start`, …)
through `streamSimple` instead of using pi-ai's `registerFauxProvider`,
which **does exist** in the source tree
(`packages/ai/src/providers/faux.ts`). That hand-rolled path will rot the
first time pi changes its event shape.

The client mixes a sync public API with a background asyncio thread for
the tool server. Works, but it's more moving parts than v2/v3/v4's
async-everywhere approach.

### v2 — `pi_python_harness` (artifacts)

The cleanest implementation overall. Two things stand out:

1. **Uses pi-ai's real `registerFauxProvider` API** (verified at
   `pi-main/packages/ai/src/providers/faux.ts`) instead of hand-rolling
   event emission. Much less likely to break on pi upgrades.
2. **The TS shim spawns the Python tool server as its own child process
   over stdio.** No TCP port, no localhost binding, no token-leak window —
   the only listener is a stdio pipe owned by the Pi subprocess. Different
   trust model from v1/v3/v4, and arguably the tightest.

Also installed the actual npm-published `@earendil-works/pi-coding-agent@0.74.0`
(matching the uploaded source's version) and ran end-to-end integration
tests against it. Not "built from source" like v1, but still real pi
exercising the real shim.

Caveat the v2 model itself flags: spawning the Python server as a Node
child means it can't easily share state with the Python parent that owns
the RPC client. Fine for the tool-execution-only scope of this prototype;
constraining if the harness later wants the parent and the shim to share
in-memory state.

### v3 — `pi-python-harness`

Did not run pi (Node 18 in its sandbox vs. pi's `>=20.6.0` requirement),
so the implementation rests on the protocol contract plus a faked-Pi
subprocess. **The design doc is the best of the four** — it's the only one
with an explicit failure-mode table, an explicit `protocolVersion` in the
manifest, and forward-looking sections on event bridges, command bridges,
state bridges, and UI bridges. If you want a writeup to base a longer-term
plan on, this is the one.

The Python ergonomics are also the richest: tool handlers can be sync,
async, return `ToolResult`/`str`/dict, or be a sync/async generator that
yields progress updates and a final result. Pleasant to write tools
against.

Cost of all that polish: no actual-pi validation, so any incorrect
assumption about pi's behavior is undiscovered.

### v4 — `pi_python_harness`

Did not run pi either, for the same Node 18 reason. The defining choice is
**HTTP/NDJSON for the tool bridge** rather than raw JSONL. Implications:

- Trivially concurrent — `ThreadingHTTPServer` handles parallel tool
  calls without effort.
- Debuggable with `curl` and friends; `/healthz` and `/manifest` are
  pleasant for ops.
- Per-call HTTP overhead is real but small.
- Pi's `AbortSignal` plugs straight into `fetch(..., { signal })`, so
  cancellation reaches the connection level naturally; the Python side
  still doesn't kill the handler, but the framing is right.
- One real wart: the handler does `asyncio.run(...)` per request, so each
  tool call spins up a fresh event loop. Fine for low rates, sloppy at
  scale.

This is the most "boring/standard" version. If a year from now someone has
to debug the bridge with packet captures or hand-issue test calls, this is
the friendliest layout.

## Cross-cutting observations

- **All four use real pi APIs.** I spot-checked `--mode rpc`,
  `--extension`, `--offline`, `pi.registerTool`, `pi.registerProvider`,
  `pi.registerCommand`, `pi.on`, the `extension_ui_request` subprotocol,
  `get_state`/`get_messages`/`get_commands`/`get_last_assistant_text`,
  `registerFauxProvider`/`fauxAssistantMessage`/`fauxToolCall`. None were
  invented.
- **All four correctly identified the RPC vs. extension split**: pi has no
  way to register a tool over RPC, so a TS extension is unavoidable; pi
  has a clean JSONL RPC, so process control is easy from Python. None
  proposed forking pi.
- **All four picked some form of bearer-token-on-loopback** as the bridge
  authn story. None proposed a Unix-domain socket as the default, though
  v1's roadmap lists it.
- **None of them implemented event/command bridges**, only tool
  execution. v3 explicitly designs the extension and is upfront that it's
  not built yet; the others are quieter about it.
- **None implemented hard cancellation of in-flight Python tool
  functions.** All can disconnect the bridge socket on Pi abort, which
  makes Python see EOF; none signal the Python coroutine. v3's design
  describes a cancellation token; nobody shipped it.
- **Schema inference is shallow.** All four cover `str/int/float/bool/list/
  dict/Optional/Union`. None cover `pydantic`/`dataclass`/`TypedDict`
  parameter types out of the box, though all allow passing an explicit
  schema. Worth flagging because, in Python, "tool author writes a Pydantic
  model" is the dominant idiom in adjacent ecosystems.

## What I'd lift from each, if combining

Picking the best parts of each:

- **From v2:** use `registerFauxProvider` (real pi API) for tests; spawn
  the Python tool server as a stdio child or, alternatively, treat it as
  v3 does (loopback TCP) but document the trade-off.
- **From v3:** the explicit `protocolVersion` in the manifest, the failure
  modes table in the doc, and the generator-based handler ergonomics.
- **From v4:** HTTP/NDJSON for the bridge if observability/debuggability
  matters more than minimal overhead; `/healthz` is cheap and useful.
- **From v1:** ship the TS shim as a static, hand-authored file inside the
  Python package rather than generating it from a Python string template
  — it keeps the TS readable, lint-able, and reviewable in isolation. Also
  steal the `/py-tools` and `/py-tool` slash commands for human
  diagnostics.

## My ranking

For "I want to actually ship this and iterate":

1. **v2** — best evidence of real-pi behavior, cleanest implementation,
   uses pi's actual faux-provider API, child-process bridge is the
   simplest trust model.
2. **v1** — strongest test demonstration (built from source, full
   agent loop), but pays for it with a hand-rolled fake provider and a
   threaded-from-sync client that's harder to maintain.
3. **v3** — best documentation and richest tool ergonomics, but no
   real-pi validation. Use the design as a north star; rebuild the
   implementation against real pi.
4. **v4** — solid, debuggable, "boring" implementation; loses to v3 on
   docs and to v1/v2 on actually-ran-pi. Worth picking if HTTP
   debuggability is a hard requirement.

## What to do next

If we're going to invest, I'd merge v2's tested core with v3's design doc:

1. Take v2's `PiRpcClient`, child-stdio tool bridge, and
   `registerFauxProvider`-based integration test as the implementation
   base.
2. Vendor v1's hand-authored TS shim as a static `.ts` file in the Python
   package (drop string-template generation) and steal the `/py-tools` and
   `/py-tool` slash commands.
3. Adopt v3's manifest `protocolVersion`, the failure-modes table, and
   the generator-based Python tool ergonomics.
4. Defer v4's HTTP bridge unless we have a concrete need for curl-level
   debuggability or external broker hosting.
5. The first real follow-on feature should be **event-bridge forwarding**
   (`pi.on("tool_call", …)` etc. → Python policy callbacks), since that's
   what unlocks "harness customization" beyond just authoring tools, and
   it's the only meaningful capability all four left as a TODO.

Before doing any of that, I'd actually run v2 here against a current pi
install on this machine (Node 20+, real LLM creds optional — the faux path
is enough for a first smoke test) to confirm the integration test passes
as claimed.
