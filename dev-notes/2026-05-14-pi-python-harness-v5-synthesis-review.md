# Review: v5 synthesis vs. v1–v4

Reviewer: Claude Opus 4.7. Date: 2026-05-14.

ChatGPT 5.5 Pro was given v1–v4 (the four parallel runs from
[2026-05-14-pi-python-harness-version-review.md](./2026-05-14-pi-python-harness-version-review.md))
and asked to audit them and produce a synthesized fifth version. The
artifact lives at
`../version-5-synthesis/pi-python-harness-synthesis/`.

This note evaluates v5 on its own merits, audits its self-audit, and
calls out what it kept, dropped, added, missed, and got wrong.

## Mapping v5's A/B/C/D back to v1–v4

The synthesis renames the four prior runs. Mapping by content:

| v5 label | My label | Tell                                                                |
| -------- | -------- | ------------------------------------------------------------------- |
| A        | v4       | "Python HTTP/NDJSON tool broker" — only v4 used HTTP                |
| B        | v3       | "async TCP JSONL tool server" + headless extension-UI handling      |
| C        | v2       | "TS extension spawns a Python tool server over stdio"               |
| D        | v1       | "static packaged TS extension" + "fake provider in shim"            |

The audit's per-attempt critiques are accurate against the actual code.
Below I use v1–v4 to stay consistent with the earlier review.

## What v5 keeps from each predecessor

- **From v1** — the TypeScript bridge shim. v5's `PRODUCTION_TS_SHIM`
  string is essentially v1's `shims/python_tools_extension.ts` with the
  env-var prefix renamed `PY_PI_*` → `PI_PY_*` and a runtime gate on the
  `/py-tools` / `/py-tool` slash commands. The bridge protocol (manifest
  / execute / update / response frames, IDs, token field) is identical
  in shape. The decorator-based `ToolRegistry`, `ToolResult`,
  `ToolContext.update`, and JSON-Schema-from-type-hints are also
  recognizably v1's design.
- **From v2** — the faux-provider integration test pattern. v5's
  `TS_FAUX_PROVIDER_TEMPLATE` is essentially v2's: it imports
  `registerFauxProvider`, `fauxAssistantMessage`, and `fauxToolCall`
  from `@earendil-works/pi-ai` rather than hand-rolling synthetic
  `streamSimple` events the way v1 did. (I verified the
  `pi-ai` exports exist at
  `pi-main/packages/ai/src/providers/faux.ts`.) The faux provider is
  also loaded as a **separate** test-only extension, not compiled into
  the production shim — addressing v1's "test code in the prod shim"
  smell.
- **From v3** — the automatic headless extension-UI handler with
  per-method overrides and a fallback, plus the strict byte-oriented
  JSONL decoder. v3's UI default policy (cancel `select`/`input`/
  `editor`, return `confirmed: false` for `confirm`, ignore the
  fire-and-forget methods like `notify`/`setStatus`/`setWidget`) is
  reproduced; I verified the method set against `rpc-types.ts:214-248`.
- **From v4** — almost nothing structural. The HTTP bridge is rejected
  (correctly, given the other three were JSONL/TCP); only the bearer-
  token-on-loopback pattern survives, and that was in v1 and v3 too. A
  fair characterization is that v4 was the loss leader of the synthesis.

## What v5 actually adds beyond v1–v4

These are not present in any of the four predecessors:

- **Cooperative cancellation.** `server.py:131-141` spawns a
  `watch_disconnect` task that reads the bridge socket; if the TS shim
  destroys the socket on `AbortSignal`, the task sets a `cancelled`
  asyncio.Event that the tool can observe via `ctx.cancelled`. None of
  v1–v4 had any cancellation propagation into Python; v3 *designed*
  one but didn't ship.
- **`hmac.compare_digest` for token comparison** (`server.py:97`) —
  constant-time, vs. v1's plain `!=`. (v4 used `secrets.compare_digest`,
  similar.)
- **Tool-name validation regex** (`tools.py:17`) — rejects names that
  pi would later refuse. None of the four did this.
- **Richer schema inference** (`tools.py:286-355`) — adds `Literal`
  (→ `enum`), `Enum` (→ `enum`), and `@dataclass` (→ nested object
  schema with required fields). v1–v4 all stopped at the
  scalar/list/dict/Optional level.
- **Bridge buffer cap** (`jsonl.py:32-45`) — `max_buffer_bytes` to
  reject unterminated JSONL frames before they exhaust memory.
- **Defensive launch flags as defaults.** `PiLaunchConfig` defaults to
  `offline=True, no_session=True, no_extensions=True, no_skills=True,
  no_prompt_templates=True, no_context_files=True`, with explicit
  opt-in for `no_builtin_tools` and `--tools <list>`. v1 had these as
  config booleans too; v2/v3/v4 were less complete on this. I verified
  `--no-builtin-tools` is a real flag at
  `pi-main/packages/coding-agent/src/cli/args.ts:106-107`.
- **Working out-of-the-box test packaging.** `pyproject.toml` declares
  `pythonpath = ["src"]` and `asyncio_mode = "auto"`. The audit
  correctly observes v3 and v4 failed `pytest` without
  `PYTHONPATH=src`. v5 fixes that.

## A real bug v5 introduced

The synthesis silently changes the RPC argument shape for `set_model`:

```python
# version-5-synthesis/.../rpc.py:301
async def set_model(self, provider: str, model: str) -> JsonObject:
    return await self.send({"type": "set_model", "provider": provider, "model": model})
```

Pi's RPC contract requires `modelId`, not `model`:

```ts
// pi-main/packages/coding-agent/src/modes/rpc/rpc-types.ts:31
| { id?: string; type: "set_model"; provider: string; modelId: string }
```

The handler explicitly errors on a missing model id:

```ts
// pi-main/packages/coding-agent/src/modes/rpc/rpc-mode.ts:457
return error(id, "set_model", `Model not found: ${command.provider}/${command.modelId}`);
```

v1's client got this field right (`{"provider": provider, "modelId":
model_id}`); v5 broke it during the rewrite. **The integration test
would not catch this**, because the test uses `--provider` and `--model`
CLI flags to pick the faux model at launch and never issues a
`set_model` RPC call. The "7 passed" claim therefore overstates the
coverage. This is a one-line fix, but it's exactly the kind of API
shape that an "audit + synthesis" pass should have preserved verbatim
from a previously-validated implementation.

## Audit-of-the-audit

A few notes on v5's own report:

- **Scoring table is overconfident.** v5 self-scores 5 across nearly
  every dimension. Cancellation gets a 4 vs. v1's 3, despite v5 still
  only providing cooperative cancellation (no hard kill, no per-tool
  subprocesses). The table reads as marketing more than analysis.
- **The "best practices" doc is largely redundant.** Most of its
  content already appears in the audit. A reader has to consult both
  to see if anything in one isn't in the other.
- **Design doc is *shorter* than v3's.** v3's design covered failure
  modes (table), explicit manifest `protocolVersion`, future event
  bridge, command bridge, state bridge, UI bridge, and a versioning
  policy. v5's design covers only the current tool-bridge scope. The
  audit credits v3 in scoring (5 for "Documentation/source grounding")
  but doesn't carry v3's forward-looking sections into the synthesis.
  This is the synthesis's biggest missed opportunity.
- **Some of the per-attempt critiques are uncharitable.** Calling
  v1's faux-provider code "test-only provider logic that shouldn't
  ship in the production bridge" reads as a real concern, but v1
  gated the faux provider on `PY_PI_FAKE_PROVIDER=1`. The criticism is
  fair structurally — code paths are clearer when fully separated —
  but the framing implies v1 was leaking test code into prod, which
  it wasn't.
- **The "stale Node 18 claim" framing is half-fair.** The synthesis was
  run in a different environment with Node 22 available, so of course
  it could install pi. v3 and v4 were stuck on Node 18 because that's
  what their sandbox had. Calling their honesty "stale" is harsh; they
  reported their environment correctly.

## What v5 still doesn't cover

- **Only the tool extension surface.** Event hooks (`pi.on(...)`),
  command registration from Python, provider registration from Python,
  and the rich UI surface are all still TS-only. v3's design called
  these out as natural next steps; v5 inherits none of those
  scaffolds. If the user's underlying goal is "extend pi entirely from
  Python," tools are roughly a third of the story.
- **No manifest `protocolVersion`.** The bridge will silently
  mis-handshake when either side updates incompatibly. v3 had this.
- **No real cancellation of in-flight Python work.** A blocking
  CPU-bound or socket-bound Python tool can't be interrupted; it just
  has to notice `ctx.cancelled` between operations. v3 designed an
  explicit cancel-tool message; v5 settles for socket EOF.
- **Provider-specific schema-compatibility tests.** Pi's validator
  accepts JSON Schema, but downstream providers (Anthropic / OpenAI /
  Google) impose their own constraints (e.g. no `nullable`, no
  unions in some positions). The audit lists this as a residual risk;
  no tests address it.
- **No event-bridge integration test.** Even though `_dispatch_event`
  and event handlers exist, there is no integration test that asserts
  Pi's `tool_execution_start` / `tool_execution_end` / `message_*` events
  reach a Python event handler in the right order. v1's testing went
  further than this.

## My read

v5 is a real synthesis, not a rewrite. It inherits v1's tested bridge
shim, v2's faux-provider test pattern, v3's headless-UI defaults, and
its own ergonomic improvements (cancellation event, dataclass-aware
schemas, name validation). The package is the cleanest of the five and
the only one whose `pytest` works out of the box without environment
setup.

The bug worth fixing immediately is the `set_model` `model` →
`modelId` field name. The mistake is small but indicative: the
synthesis's claim to be "tested against real Pi" is true only of the
launch / tool-loop / faux-provider path. Any RPC command not exercised
by the integration test is no more trustworthy in v5 than in v1.

The bigger structural critique is what's *not* in v5: v3's design
breadth. The audit identified v3's docs as the best of the four but
then collapsed the synthesis to v1's tool-only scope plus polish. A
combined "v5 implementation + v3 design doc" would have been the
strongest pair to hand off.

## Ranking, updated

For "I want to ship and iterate on this":

1. **v5** — best implementation. Worth fixing the `set_model` bug and
   adopting v3's design doc as the planning document.
2. **v2** — second-best, especially if the "TS spawns Python" lifecycle
   inversion is acceptable. Strongest evidence of real-pi behavior
   relative to its scope.
3. **v1** — strongest "actually exercised the full agent loop" test
   demonstration; its shim is what v5 effectively re-uses.
4. **v3** — best design doc, weakest tested code. Use the docs.
5. **v4** — solid debuggable HTTP variant; loses to v5 on every axis
   v4 was good at.

## Concrete follow-ups before adopting v5

1. Fix `rpc.py:301-302`: change `"model": model` to `"modelId": model`.
2. Add an integration test that calls `client.set_model(...)` after
   startup against the faux provider — this is the kind of bug that
   should not survive a "synthesis" pass twice.
3. Adopt v3's manifest `protocolVersion` field; add a handshake check
   in the TS shim.
4. Take v3's design doc, retitle it for v5, and use it as the planning
   doc for the event/command/provider/UI bridges that the synthesis
   left out.
5. Confirm in this environment (Node 20+) that
   `PI_CLI=$(npm root -g)/@earendil-works/pi-coding-agent/dist/cli.js
   python -m pytest -q` actually goes green before reading further
   confidence into the "7 passed" claim.
