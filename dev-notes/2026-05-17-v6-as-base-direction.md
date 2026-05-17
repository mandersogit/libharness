---
status: In co-design
created: '2026-05-17'
---

# v6-threaded as a new baseline — direction discussion

A hypothetical-but-take-it-seriously discussion document: **if** the asyncio → threads rewrite plan (`dev-notes/2026-05-15-threads-rewrite-plan.md`) were superseded by adopting `dev-notes/predecessors/v6-threaded/` as the new baseline, what would that look like?

Context: an in-progress implementation of the threads-rewrite plan exists on the "spark" workstream (not in this checkout). As that work proceeded, doubts surfaced about whether the rip-asyncio-out direction is the right one. This document presents v6 as a candidate alternative — explicitly the path the 2026-05-15 concurrency-model discussion called option **B** ("sync facade over async core") and rejected. The reboot is not committed; this is a "let's think it through honestly" document.

Companion to `dev-notes/2026-05-17-v6-threaded-comparison.md` (which compares v6 to current and the rewrite plan in detail). That doc analyzed v6 *as a predecessor to look back at*; this doc treats v6 *as a forward direction to develop from*.

## What we'd be saying yes to

By picking v6, we'd be choosing:

1. **Asyncio stays inside.** `PiRpcClient`, `PythonToolServer`, the `_handle_message` reader, the bridge socket I/O — all asyncio coroutines on a dedicated loop thread.
1. **Threads sit on the outside.** A `HarnessRuntime` owns the loop thread + a shared `ThreadPoolExecutor` for tool execution. Each `PiAgentHarness` is a proxy that has its own owner thread for harness state, marshalling work to the runtime loop via `concurrent.futures.Future`.
1. **The caller surface is sync end-to-end.** Application code does `harness.start()`, `harness.prompt(...)`, `harness.get_state()` from any thread, including `MainThread`. No `async with` / `await` leaks to the user.
1. **Multi-harness embedding is a first-class shape.** N `PiAgentHarness` instances can share one `HarnessRuntime` (one loop thread, one tool pool, one place to set concurrency limits).
1. **Tool authoring stays permissive.** Sync `def`, `async def`, sync generators, async generators — all accepted. v6 carries v5's `collect_tool_result` verbatim.

What we'd be saying no to:

- The "rip asyncio out, sync end-to-end" direction recorded as the 2026-05-15 decision.
- The FT-first framing (3.14t freethreading as primary verification surface).
- The threads-rewrite plan's ~1100-LOC rewrite of `rpc.py` + `server.py` + `tools.py` + `harness.py`.
- "`async def` rejected at decoration" (not-pi-2 pattern). v6 accepts both.
- Sync `on_*` hooks dispatched directly from the reader thread (the rewrite plan's § Design answers #6 architecture).

## Why we might want to do this

The threads-rewrite plan's strongest argument was the `on_*` hooks color problem: "every subclasser writes `async def on_tool_call` even though the body has no `await`s." That is real. But it's a problem about the **caller-facing hook API**, not the **internals**. The internals can stay async; the hook API can still be sync if we dispatch hooks through an executor (see § Hook dispatch design below).

Independent reasons to favor v6:

- **Code preservation.** The asyncio core is already well-tested. `PiRpcClient`, the bridge server, the JSONL decoder, the shim handshake — these are mature. Rewriting them to threads is high-risk-low-reward.
- **Pi's natural model is async.** Pi (Node) is event-loop-driven. Mirroring that in Python at the I/O layer (subprocess + sockets) reduces impedance: `await reader.readline()` is structurally similar to how pi reads its own input.
- **Embedding posture.** Applications that embed libharness (e.g. a web service, a long-running agent host) typically want their main thread free for their own event loop or request dispatcher. v6's "library owns its loop thread; you keep yours" matches that. The rewrite plan's "block the caller's thread on `harness.client.prompt()`" forces the application to manage that blocking.
- **Multi-harness ergonomics.** If we ever run N agents in one process (a swarm, a comparison run, a hosted multi-tenant tool), v6's `HarnessRuntime` already has the scaffolding. The rewrite plan deferred multi-harness ("TBD").
- **Less code churn.** ~600 LOC of new infrastructure (`runtime.py` + `agent.py`) on top of v5, vs ~600 LOC of rewritten internals plus carefully migrated tests in the threading direction. The total LOC delta is similar but the v6 path is *additive*, not *replacement*.
- **Spark experience signal.** The author has been implementing the threading rewrite on spark and developed doubts. That signal matters: if the rewrite *feels* like fighting the existing code, that's data.

## What we'd be losing

Be honest about what the threading rewrite buys that v6 doesn't:

- **Conceptual simplicity.** "It's all just threads and blocking calls" is easier to reason about than "an asyncio loop in a dedicated thread plus a tool pool plus per-harness owner threads."
- **Family consistency.** hildy, simple-harness, not-pi-2 are all sync. Libharness on v6 would be the outlier in the family.
- **Sync `on_*` hooks without extra machinery.** Under the rewrite plan, hooks are sync calls on the reader thread, full stop. Under v6, sync hooks require an executor hop or a "dispatch via the tool pool" decision.
- **FT-first story.** The dual-venv scaffolding (3.11 + 3.14t) was built specifically for the FT-first goal. Under v6, FT speedups apply only to the tool pool's parallel execution, not to the I/O core. Still real but smaller.
- **`async def` rejection.** Under the rewrite, decorating an `async def` tool fails at registration with a remediation message. Under v6, async tools work — which is friendly but also lets users write `async def` tools without realizing they're getting an extra layer of indirection.

## What's already in current libharness that we'd carry forward

Adopting v6 as the new base does *not* mean reverting to v6's exact code. The current `src/libharness/pi/` has already fixed two real bugs and added a real safety mechanism on top of v5; v6 doesn't have those because v6 was generated from the same v5 baseline in parallel.

| Improvement                       | Where it lives now                           | Must preserve under v6 |
| --------------------------------- | -------------------------------------------- | ---------------------- |
| `set_model` → `modelId` wire fix  | `src/libharness/pi/rpc.py:300-303`           | Yes                    |
| Manifest `protocolVersion` field  | `src/libharness/pi/tools.py:26`              | Yes                    |
| Manifest version check in shim    | `src/libharness/pi/shim.py:185-194`          | Yes                    |
| Pi-internals reference notes      | `dev-notes/2026-05-14-pi-internals-notes.md` | No code change         |
| Dual-venv scaffold (3.11 + 3.14t) | `Makefile`, `local.venv/`, `local-ft.venv/`  | Decide (see § FT)      |

**Detail:**

- *`set_model` fix:* v6 has the v5 bug (`{"type":"set_model","provider":..., "model":...}`). pi-mono's `rpc-types.ts:31` requires `modelId`. Current libharness fixed this with a regression test (`tests/pi/test_set_model.py`). Must port forward.
- *Manifest `protocolVersion`:* current adds `MANIFEST_PROTOCOL_VERSION = 1` to `tools.py` and the corresponding assertion in the TS shim. Catches drift between Python and TS at extension load. v6 doesn't have this. Must port forward.
- *Pi-internals reference notes:* a knowledge asset, not code. Unaffected by the choice.
- *Dual-venv scaffold:* the cost is sunk; the *value* depends on whether we still target FT. See § FT below.

## Hook dispatch design under v6

This is the deepest design question, because it's the one that the concurrency-model discussion called out as the deciding factor.

Recall the problem from `2026-05-15-concurrency-model-discussion.md` § "Why the `on_*` hooks design effectively forces the answer":

> Under asyncio, every overrideable hook must be `async def`. A subclasser who wants `on_tool_call` to log a line writes `async def on_tool_call(self, event): logger.info(...)`. That `async` is wrong-shaped: there's no `await` in the body. The user has to know they need it.

Under v6, three viable shapes for the `Agent.on_*` hook API:

### Option H1: hooks are `async def`, called from the loop thread

```python
class MyAgent(Agent):
    async def on_tool_call(self, event):
        logger.info("tool call: %s", event.name)
```

- **Pro:** simplest internal plumbing — `_dispatch_event` already runs on the loop, just `await handler(event)`.
- **Pro:** matches the surrounding asyncio idiom.
- **Con:** exactly the color-leak problem flagged in the concurrency discussion. Wrong-shaped for casual subclassers.
- **Con:** mixed sync/async hook bodies are easy to mistake (returning a coroutine that's never awaited).

### Option H2: hooks are `def`, dispatched via `loop.run_in_executor(hook_pool, ...)`

```python
class MyAgent(Agent):
    def on_tool_call(self, event):
        logger.info("tool call: %s", event.name)
```

Internally: `_dispatch_event` does `await loop.run_in_executor(self.hook_executor, self._invoke_hook, event)`.

- **Pro:** sync hook bodies; no color leak.
- **Pro:** hook work doesn't block I/O readers — the loop thread `await`s the executor future and stays free.
- **Con:** event ordering: if two events arrive back-to-back, both `run_in_executor` schedule, both run in parallel. Per-event ordering needs an explicit serialization mechanism (a per-agent queue, or `max_workers=1` for the hook pool).
- **Con:** an extra thread pool. Could reuse the tool executor, but then a slow tool starves event delivery.
- **Con:** decision events (where pi blocks waiting for a Python response, e.g. permission gates) get harder — the loop awaits the executor result anyway, so it's fine, but the path is longer.

### Option H3: hooks are `def`, called *directly* from the loop thread (blocking the loop)

```python
class MyAgent(Agent):
    def on_tool_call(self, event):
        logger.info("tool call: %s", event.name)
```

Internally: `_dispatch_event` calls `handler(event)` directly (no `await`).

- **Pro:** sync hook bodies, no extra machinery.
- **Pro:** event ordering is automatic (one event at a time on the loop).
- **Con:** a slow hook (e.g. a hook that does a 100ms HTTP call) freezes I/O for that duration — the same constraint the threads-rewrite plan calls out for its reader-thread dispatch.
- **Con:** if a hook accidentally calls something that needs the loop (e.g. another `await client.send(...)`), it deadlocks. The threads-rewrite version doesn't have this footgun.

### Recommendation (original)

**Default to H3, with a documented opt-in to H2 for hooks that need to do real I/O.**

H3 matches the threads-rewrite plan's contract: the dispatch site blocks during the hook, decision events get synchronous results, notification events stay fast. The risk (slow hook freezes I/O) is identical to the rewrite plan and has the same mitigation ("don't do real work in hooks; if you must, dispatch to a worker"). Document the deadlock footgun and provide a `harness.submit(coro)` escape hatch.

For comparison: option H1 (async hooks) is what v6 would give you implicitly today via `client.on_event(handler)` if you let `handler` be a coroutine. That path stays available; the *new* surface (the `Agent` class) defaults to sync.

This means: **the `on_*` hooks problem is solvable under v6 without giving up the async core.** It requires one extra mechanism (call the sync handler from within an `async def _dispatch_event` — trivial), not a rewrite. This is probably the load-bearing realization for choosing v6.

### Option H1-layered (added 2026-05-17, author input)

A refinement on H1 proposed in co-design: every hook is *two* methods.

- `_async_on_{event}` — the async layer, called by `_dispatch_event` on the loop thread. **Base-class default** calls `self.on_{event}(event)` and returns.
- `on_{event}` — the sync layer. **Base-class default** is `pass`. This is what casual subclassers override.

Code sketch:

```python
class Agent:
    async def _async_on_tool_call(self, event: ToolCallEvent) -> None:
        # default: delegate to the sync hook
        self.on_tool_call(event)

    def on_tool_call(self, event: ToolCallEvent) -> None:
        # default: do nothing
        pass
```

Casual subclasser:

```python
class MyAgent(Agent):
    def on_tool_call(self, event):
        log.info("tool call: %s", event.name)
```

Advanced subclasser (who needs to do async work in the hook):

```python
class MyAdvancedAgent(Agent):
    async def _async_on_tool_call(self, event):
        # do async work that the loop is best-positioned to run
        result = await self.client.send({"type": "extension_ui_request_response", ...})
        # still invoke the sync hook for ergonomic logging etc.
        self.on_tool_call(event)
```

**Pro:** casual subclassers see only `on_X` (sync, no color leak).
**Pro:** advanced users have a documented escape hatch without monkey-patching.
**Pro:** internal plumbing is the cleanest H1 (`await self._async_on_tool_call(event)` inside the async dispatcher). No executor needed.
**Pro:** every hook has the same structural shape; mypy sees both layers; subclasses are obvious to readers.
**Con:** doubles the number of methods on `Agent` (N hooks × 2 = 2N methods).
**Con:** documentation has to explain both layers and when to override which.
**Con:** if the casual sync `on_X` does slow work, it still blocks the loop (because the default async layer calls it directly). The H3 blocking concern survives in the default path.

### Open sub-decision: default-layer behavior

If we adopt H1-layered, the base-class default of `_async_on_X` has two viable shapes:

**Variant Default-A — synchronous delegation (H3-flavored):**

```python
async def _async_on_tool_call(self, event):
    self.on_tool_call(event)
```

- *Pro:* trivial; one call site; preserves event ordering on the loop.
- *Con:* a slow sync `on_X` blocks the loop. Same footgun as plain H3.

**Variant Default-B — executor delegation (H2-flavored):**

```python
async def _async_on_tool_call(self, event):
    await self._loop.run_in_executor(self._hook_executor, self.on_tool_call, event)
```

- *Pro:* sync hooks can do real work without freezing I/O.
- *Con:* needs a `_hook_executor` (probably `max_workers=1` to preserve ordering); one extra hop per event.
- *Con:* if `_hook_executor` is shared with the tool pool, a slow tool starves hook delivery.

**Recommendation (Opus, medium confidence):** Default-A. The simplicity is worth the latency-only-for-slow-hooks tradeoff, and casual hooks (the 95% case) are fast. Document the "don't do slow work in `on_X`" rule and provide one of two escape hatches: (i) override `_async_on_X` to do explicit `run_in_executor`, or (ii) hand off to `harness.submit(coro)` from inside the sync hook. The "if you need the executor, opt in via override" framing keeps the base case clean.

If we expect lots of slow-hook footguns in practice (e.g. users habitually putting HTTP calls in `on_tool_call`), flip to Default-B. This is a real judgment call about expected user behavior.

### Pushback (one point I want to flag)

> If you still want to proceed, I'll proceed — but flagging this once.

**Are we sure we need the `_async_on_{event}` layer at all in v1?**

The H1-layered design is elegant, but the advanced override is a *capability* without a concrete *use case* in the document. The 95% case is `def on_tool_call(self, event): log.info(...)`. The 5% case ("I need to do async work in this hook") is hypothetical right now.

Alternatives that ship less surface area now:

1. **Sync-only v1.** Ship just `def on_{event}`, called from `_dispatch_event` via `loop.run_in_executor` *or* directly (pick one). No `_async_on_X` exists. If/when a real use case appears for inline async work in a hook, add `_async_on_X` then.
1. **One-method overload.** `on_{event}` is the single method; accept either `def` or `async def`. `_dispatch_event` introspects the override with `inspect.iscoroutinefunction(self.on_X)` and `await`s if needed. Same advanced-user reach, half the surface. Mirrors the v5 tool-registration permissiveness.
1. **H1-layered as proposed.** Ship both methods. Surface is bigger now, but the structure is uniform and the advanced-override seat is reserved.

The "one-method overload" variant is appealing because:

- Casual user: `def on_X(self, event): ...`.
- Advanced user: `async def on_X(self, event): await ...`.
- Internal: `if iscoroutinefunction(handler): await handler(event) else: handler(event)`.
- Half the surface; one method per event.

The cost: `inspect.iscoroutinefunction` on every dispatch (microseconds; cache it on first call). And the subtle danger that someone writes `def on_X(self, event): return some_coro()` and the coroutine is never awaited — `inspect.iscoroutinefunction` catches the `async def` case but not the "returns a coroutine from a sync function" case. The H1-layered design also doesn't catch this; you'd need an `inspect.isawaitable(result)` check on the return value to do that.

**My recommendation (Opus, medium confidence):** ship "one-method overload" first. It's the minimum viable Agent class. Promote to H1-layered if and when we hit a real case where the advanced user wants both layers active at once (calling `super()._async_on_X(...)` in their override, etc.). Until then, the two-method design is speculative API surface.

If the author's stated intent ("subclasser sees simple sync; advanced user can drop to async") is structural — i.e., a stable contract we want to commit to early so it's part of the v1 API — then H1-layered is correct and worth the surface area. The pushback is real, but ship-H1-layered is a defensible answer to it.

### Option MCA — parallel two-method dispatcher (author proposal)

A fourth option, structurally distinct from H1-layered and from one-method-overload. Each event has **two parallel hook names**, `async_on_{event}` and `on_{event}`. The subclasser defines whichever they need (typically just one). The dispatcher introspects at runtime: async wins if defined, else sync goes through `run_in_executor`, else (optionally) raise.

```python
async def _async_on_event(self, event):
    name = event.name
    async_handler = getattr(self, f"async_on_{name}", None)
    if async_handler:
        await async_handler(event)
        return
    sync_handler = getattr(self, f"on_{name}", None)
    if sync_handler:
        await self._loop.run_in_executor(self._hook_executor, sync_handler, event)
        return
    # whatever happens when no handler is defined. Nothing? Error?
    if self._raise_on_no_event_handler:
        raise TheExceptionType(f"no handler defined for event of type {name}")
```

**How this differs from the earlier options:**

- *vs H1-layered:* the two methods are **parallel, not chained.** The async layer does not delegate to the sync layer; the dispatcher picks one or the other. The subclasser defines exactly one.
- *vs one-method overload:* names are distinct (`async_on_X` vs `on_X`), so the choice is explicit at the call site, not detected by introspection of a single name. No silent "I wrote `def` but returned a coroutine" failure mode (the user simply never picked the async name).
- *vs sync-only:* the async escape hatch is built in from v1 — but only pays surface area for events the subclasser actually overrides.

**Pro:** zero "speculative API" cost. Default `Agent` has no `on_X` and no `async_on_X` defined; nothing exists until the user adds it. The base class is genuinely small.
**Pro:** sync hooks run through `run_in_executor` by default → they cannot freeze the I/O loop. Decision-event latency is one thread hop (≈10μs) — negligible.
**Pro:** the user picks the color at write time by choosing a name. No mental overhead about "which one am I overriding?"
**Pro:** the `_raise_on_no_event_handler` flag gives an opt-in safety net for typos and untaught events.

**Con:** `getattr(self, name, None)` on every dispatch. Cache by event-type on first lookup to keep cost flat.
**Con:** can't easily combine "fast sync logging + slow async work" for the same event — the user picks one path. Mitigation: from inside `async_on_X`, just call your own sync logger. The dual-method requirement is gone, but the user can structure their handler however they want.
**Con:** `async_on_X` and `on_X` both defined on the same class is ambiguous. Dispatcher picks async; document as "if both exist, async wins." Or refuse to start with a clear error. Lean toward the latter for safety.
**Con:** subclass MRO interactions need thought. If `class A(Agent)` defines `on_X` and `class B(A)` defines `async_on_X`, MCA picks async (correct: most-derived signature wins). If a deeper subclass wants to call the parent sync logic, it does `self.on_X(event)` directly — slightly unobvious but workable.

**Implicit decisions MCA bakes in:**

- *Default-B (executor delegation) for the sync path.* Sync hooks always run via `run_in_executor`. Not Default-A.
- *Distinct method names per color.* Not introspection-based dispatch on a single name.
- *Hook executor is a thing.* Needs sizing — see § Hook executor sizing below.

### Hook executor sizing (relevant for MCA / Default-B)

If sync hooks run via `run_in_executor`, the executor's `max_workers` affects ordering and parallelism:

| Configuration              | Behavior                                                         |
| -------------------------- | ---------------------------------------------------------------- |
| `max_workers=1`            | Strict per-agent ordering; no parallel hooks                     |
| `max_workers=N > 1`        | Parallel hooks; ordering preserved only by `await` in dispatcher |
| Reuse tool pool            | Hook + tool contention; bad isolation                            |
| Dedicated `_hook_executor` | Clean isolation; one more thread pool per harness/runtime        |

Because the dispatcher does `await self._loop.run_in_executor(...)` and then returns, each event is awaited before the next dispatches. Ordering is preserved regardless of `max_workers`. So `max_workers=N` is fine as long as the dispatcher serializes; the benefit of `>1` is only if we want to fan out *within* a single hook (which we don't).

**Recommendation:** dedicated `_hook_executor` per `HarnessRuntime`, `max_workers=1`. Cheapest, safest, no contention with tool pool.

### Option MCA-D — MCA with declared base attributes (author refinement)

A refinement on MCA: instead of `getattr(self, name, None)` lookups at dispatch, **declare every `async_on_{event}` and `on_{event}` as a class attribute on `Agent`, defaulted to `None`**. Dispatcher does direct attribute access (no `getattr`). Only one of the two may be non-`None` for a given event on any concrete class; this is checked at class construction.

```python
class Agent:
    # All known events declared. Adding a new event = editing this list.
    _EVENT_NAMES: ClassVar[tuple[str, ...]] = (
        "tool_call",
        "tool_result",
        "agent_start",
        "agent_end",
        "message_delta",
        # ...
    )

    # Default surface (auto-populated via __init_subclass__-style metaclass or codegen):
    async_on_tool_call: ClassVar[AsyncHandler | None] = None
    on_tool_call: ClassVar[SyncHandler | None] = None
    async_on_tool_result: ClassVar[AsyncHandler | None] = None
    on_tool_result: ClassVar[SyncHandler | None] = None
    # ... etc.

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name in cls._EVENT_NAMES:
            a = getattr(cls, f"async_on_{name}", None)
            s = getattr(cls, f"on_{name}", None)
            if a is not None and s is not None:
                raise TypeError(
                    f"{cls.__name__} defines both async_on_{name} and on_{name}; "
                    f"define exactly one, or explicitly set the other to None."
                )

    async def _async_on_event(self, event):
        name = event.type
        async_handler = self.__class__.__dict__.get(f"async_on_{name}")  # or MRO-aware
        # In practice: direct attribute access works, but we need bound-method semantics.
        # Simplest correct form:
        async_handler = getattr(self, f"async_on_{name}")
        if async_handler is not None:
            await async_handler(event)
            return
        sync_handler = getattr(self, f"on_{name}")
        if sync_handler is not None:
            await self._loop.run_in_executor(self._hook_executor, sync_handler, event)
            return
        # event has no handler on this Agent; silently ignore (default) or raise (strict).
```

Subclasser usage:

```python
# Common case: just define what you need
class MyAgent(Agent):
    def on_tool_call(self, event):
        log.info("tool: %s", event.name)

# Switch color in a deeper subclass
class MyAdvancedAgent(MyAgent):
    on_tool_call = None  # explicitly clear inherited sync handler
    async def async_on_tool_call(self, event):
        await self.do_async_thing(event)
```

**How MCA-D differs from MCA:**

| Aspect                                | MCA (getattr-based)             | MCA-D (declared-None)            |
| ------------------------------------- | ------------------------------- | -------------------------------- |
| Hook discoverability via `dir(Agent)` | hidden                          | visible                          |
| IDE autocomplete on subclass          | weak                            | strong                           |
| Type-checker view of the surface      | opaque                          | typed                            |
| Both-defined check                    | runtime (first event)           | class construction               |
| Adding a new event type               | teach subclass + (no base edit) | edit `_EVENT_NAMES` on base      |
| Typo'd hook name                      | silent (never called)           | silent (still not in event list) |
| Subclass "switch color"               | define the other, ignore old    | explicit `old_name = None`       |
| Direct attribute access at dispatch   | n/a — getattr always            | yes (bound method via getattr)   |

(Note: even MCA-D uses `getattr(self, name)` for bound-method semantics — `self.on_tool_call` is the right call site, not raw class-dict access. The "direct" vs "via getattr" distinction is about *whether the name might not exist* — under MCA-D it always exists, defaulted to `None`. Lookup behavior is identical, but `getattr` no longer needs a default value and the absence of a default catches "user typo'd `async_on_tool_cal`" because the typo's attribute is never read.)

**Pro:** the full hook surface is *declared and discoverable*. `dir(Agent)` shows every event we promise to dispatch.
**Pro:** type checkers can verify that the user assigned a method (not a string or int) to a known hook attribute.
**Pro:** "both defined" is caught at class construction, not at first event.
**Pro:** "switch color" is a one-liner: assign `None` to the inherited name.
**Pro:** adding a new event to the contract is an *explicit* base-class change — forces the contract to be considered (signature, when it fires, what fields the event has).
**Pro:** the `__init_subclass__` hook can also validate that anything looking like `async_on_*` or `on_*` on the subclass is in `_EVENT_NAMES` — catching the typo'd-hook footgun that MCA can't.

**Con:** base class is verbose. 2N class attributes for N events (N is probably 10–20 from pi's current event surface). Mitigation: generate them at metaclass time from `_EVENT_NAMES` to keep the source readable.
**Con:** adding a new event to the contract means a base-class edit — slower than "just teach your subclass." But this is the right friction (see § "Adding a new event" below).
**Con:** the typed `AsyncHandler | None` / `SyncHandler | None` class-attribute annotation is awkward — mypy doesn't love `Optional` callables-as-class-attrs that get overridden by methods. Likely needs `# type: ignore` in places or a `Protocol` definition. Workable but requires care.
**Con:** the `_EVENT_NAMES` list creates a sync-point between this base class and pi's event taxonomy. If pi adds an event type, the base class needs to catch up.

### Adding a new event under MCA-D

Suppose pi adds a `session_renamed` event in a future version. Under MCA-D, the steps are:

1. Add `"session_renamed"` to `Agent._EVENT_NAMES`.
1. Add `async_on_session_renamed: AsyncHandler | None = None` and `on_session_renamed: SyncHandler | None = None` to `Agent` (or have the metaclass auto-populate).
1. Existing user subclasses keep working (both attributes inherit `None`; dispatcher no-ops).
1. User who wants to handle the new event subclasses and defines `def on_session_renamed`.

The "edit base class on new event" friction means **we're consciously choosing which events to expose as named hooks.** Events we *do* receive from pi but haven't declared a hook for fall through to the underlying `client.on_event(handler)` plumbing — they're still observable, just not via a typed `Agent` method. That's the right behavior: not every event needs a first-class hook in v1.

### Updated recommendation (post-MCA-D)

With MCA-D in the mix, the ranking shifts again. MCA-D keeps every benefit MCA brought (parallel two-method shape, async escape hatch, sync handlers off the loop) and adds three real wins: static discoverability, class-construction error checking, and typo-catching via `__init_subclass__`. The cost is base-class verbosity — solvable with a small amount of metaclass codegen.

**Revised recommendation (Opus, high confidence):** MCA-D.

Comparison summary (across all four):

| Aspect                        | Sync-only | One-method overload | H1-layered | MCA       | MCA-D     |
| ----------------------------- | --------- | ------------------- | ---------- | --------- | --------- |
| Async escape hatch in v1      | No        | Yes                 | Yes        | Yes       | Yes       |
| API surface per event (base)  | 1         | 1                   | 2          | 0         | 2 (None)  |
| API surface per event (sub)   | 1         | 1                   | 2          | 0/1/2     | 1\*\*     |
| Sync blocks loop              | depends   | Yes                 | Yes (def)  | No        | No        |
| Color picked at write time    | n/a       | by `async` keyword  | by method  | by name   | by name   |
| Silent "missed await" footgun | n/a       | possible            | possible   | unlikely  | unlikely  |
| Typo'd hook caught            | n/a       | no                  | no         | no        | yes       |
| Both-defined caught           | n/a       | n/a                 | n/a        | dispatch  | class def |
| Hook executor required        | no/yes    | no                  | no         | yes       | yes       |
| IDE autocomplete on subclass  | yes       | yes                 | yes        | weak      | strong    |
| Adding a new event type       | edit base | edit base           | edit base  | no edit\* | edit base |

\*MCA can technically dispatch any event name a subclass defines a hook for, even ones we don't formally list. This sounds like a pro until you realize it means there's no canonical list of supported events.
\*\*Under MCA-D the subclass overrides exactly one of the two declared base attributes per event; the other inherits its base `None`. So the **effective** surface per event is 1 (the one the subclass actually overrode), even though both names are declared.

**Detail per row (new and revised):**

- *Typo'd hook caught:* MCA-D's `__init_subclass__` can verify every `async_on_*` / `on_*` attribute on the subclass corresponds to a name in `_EVENT_NAMES`. The other options either don't have the validation seat or don't know the full event list.
- *Both-defined caught:* MCA defers the check to first dispatch (and "async wins" is a runtime decision). MCA-D catches it at class definition — `class MyAgent(Agent): ...` raises immediately if both are non-None.
- *IDE autocomplete on subclass:* MCA-D's declared attributes are visible to language servers; the user starts typing `async_on_` and gets the full list. MCA's getattr-based dispatch is invisible.
- *Adding a new event type:* MCA-D requires a base-class edit (add to `_EVENT_NAMES`, attributes auto-populate). MCA technically doesn't — but that's a footgun, not a pro.

**My remaining concerns about MCA-D:**

1. *The `_raise_on_unhandled_event` flag* (was `_raise_on_no_event_handler`) is still a tempting default-on that I want to default-off. Pi adds new event types over time; strict-by-default breaks upgrades. Opt-in for users who want it.
1. *Base class verbosity* is real. If we don't auto-populate via a metaclass / `__init_subclass__` builder, the base class has 20+ literal `async_on_X = None` lines plus 20+ `on_X = None` lines. Readable but noisy. Recommendation: generate them at metaclass time so the source declares only `_EVENT_NAMES` and a `_HookProtocol` once.
1. *Class-attribute method semantics.* Declaring `on_tool_call: SyncHandler | None = None` and then subclass-defining `def on_tool_call(self, event): ...` works in Python (the method replaces the class attribute), but the typing story is awkward. Likely needs a `Protocol` and a `cast` somewhere, or `# type: ignore[assignment]`. Workable, not pretty.

### Decision needed

1. **API surface:** sync-only, one-method-overload, H1-layered, MCA, or **MCA-D** (current recommendation)?
1. **If H1-layered or one-method-overload picked:** Default-A (sync delegation, blocks loop) or Default-B (executor delegation, decouples)? (MCA and MCA-D bake Default-B in.)
1. **If MCA or MCA-D picked:** strict mode (raise on unhandled event) default on or off? Recommendation: off.
1. **If MCA-D picked:** declare the base attributes literally (verbose source, simple to read) or auto-generate via a metaclass from `_EVENT_NAMES` (terse source, one indirection to understand)? Recommendation: metaclass auto-generation once `_EVENT_NAMES` has more than ~6 entries.

All decisions go into `dev-notes/2026-05-17-agent-class-design.md` once made. Until then, this section is the source of truth.

## Concurrency-model decision: revisit or override?

The 2026-05-15 decision says:

> Primarily **D** — threads on freethreaded CPython 3.14t. Backwards-compatible with **C** — threads on standard CPython 3.11+ under the GIL.

Adopting v6 is choosing option **B** (sync facade over async core) — explicitly rejected on 2026-05-15.

Two ways to handle this:

1. **Reopen the decision.** Mark the 2026-05-15 entry as "Superseded by 2026-05-17" in `docs/DESIGN.md` § Resolved decisions. Add a new row dated 2026-05-17: "Concurrency: option B (asyncio core in dedicated thread, sync caller facade)." Keep the original entry; never delete.
1. **Frame it as a refinement, not a reversal.** Argue that "option B with sync `on_*` hooks via H3" is materially different from option B as originally evaluated (which assumed async hooks). The concurrency-discussion writeup considered B "doesn't fix the `on_*` hooks problem unless we also rewrite the Agent class as sync (at which point the async core isn't earning its keep)." This document's claim is precisely that the async core *does* earn its keep, even with sync hooks, because (a) it preserves a mature codebase, (b) it gives a natural multi-harness story, (c) it doesn't force the caller to give up MainThread.

I think #1 is cleaner — it's honest about what's happening. The 2026-05-15 decision was made in good faith and the new argument is "we tried implementing D on spark and found friction we didn't anticipate." That's exactly why decisions get revisited. Mark old, add new, write the rationale.

## What about freethreading?

The FT-first framing was the strongest pro for option D. Under v6, that framing softens but doesn't vanish:

- **What FT still buys under v6:** parallel tool execution in the shared `ThreadPoolExecutor`. When pi calls two `executionMode: "parallel"` tools concurrently, v6 already routes them to two pool threads. On 3.11 they take turns at the GIL; on 3.14t they run truly in parallel if CPU-bound.
- **What FT doesn't buy under v6:** the I/O core (asyncio loop, subprocess reader, bridge socket reader) stays on one thread. Async coroutines on a single loop don't benefit from FT — there's no other thread to run on.
- **Net:** FT speedups apply to user tools, not to libharness internals. That's still useful but smaller than the rewrite plan's "everything is threads, everything can parallelize on FT" framing.

Practical question: **do we keep the dual-venv?**

| Option             | Pro                          | Con                  |
| ------------------ | ---------------------------- | -------------------- |
| Both in `make all` | FT verified always           | 2x wall time         |
| 3.11 only          | Simplest                     | FT verification lost |
| 3.14t opt-in       | `make all-ft` on demand only | Drift risk           |

**Detail:**

- *Both in `make all`:* keep the existing scaffold as-is. Pro: parallel-tool FT speedups stay verified on every commit; future-proofs us if FT becomes a primary goal again. Con: every CI run pays 2x wall time and the two-venv complexity stays in the Makefile and scripts.
- *3.11 only:* drop `local-ft.venv` entirely. Pro: simplest possible setup; matches v6's stated 3.10+ floor. Con: lose FT verification for the tool pool (the one place FT still buys us something under v6); have to re-scaffold from scratch if FT becomes a goal again.
- *3.14t opt-in:* keep `local-ft.venv` but make `make all` use only 3.11; expose `make all-ft` for the dual run. Pro: scaffolding is paid for; CI can run FT on a slower cadence (nightly, on PRs that touch tool-pool code). Con: a path nobody runs by default rots — the 3.14t venv quietly breaks because nobody notices.

**Recommendation:** keep dual-venv but make 3.11 primary, 3.14t opt-in (`make all` runs 3.11, `make all-ft` runs both). The scaffolding cost is already paid; we'd just be flipping which venv is the default.

## What changes vs the current state

| File / area                                            | Change                                                         |
| ------------------------------------------------------ | -------------------------------------------------------------- |
| `src/libharness/pi/__init__.py`                        | Re-export `PiAgentHarness`, `HarnessRuntime`                   |
| `src/libharness/pi/agent.py` (new)                     | Port from v6                                                   |
| `src/libharness/pi/runtime.py` (new)                   | Port from v6                                                   |
| `src/libharness/pi/server.py`                          | Add `tool_executor` ctor arg (from v6)                         |
| `src/libharness/pi/tools.py`                           | Add `freeze()` / lock (from v6); keep `protocolVersion`        |
| `src/libharness/pi/rpc.py`                             | Keep mostly as-is; keep `set_model` fix                        |
| `src/libharness/pi/harness.py`                         | Keep `PiPythonHarness` as async-first power-user surface       |
| `tests/pi/test_threaded_agent.py` (new)                | Port from v6                                                   |
| `docs/DESIGN.md`                                       | Major edit: architecture section, decision register            |
| `dev-notes/SESSION-STATE.md`                           | Major edit: current state, pending tasks                       |
| `dev-notes/2026-05-15-threads-rewrite-plan.md`         | Mark Superseded                                                |
| `dev-notes/2026-05-15-concurrency-model-discussion.md` | Add 2026-05-17 update; original Resolution becomes historical  |
| `dev-notes/2026-05-17-v6-as-base-direction.md`         | This file                                                      |
| `dev-notes/2026-05-17-agent-class-design.md` (new)     | Specify the `Agent`-with-`on_*` shape under v6                 |
| `Makefile`                                             | Default `make all` → 3.11; `make all-ft` opt-in (or no change) |

**Detail:**

- *`pi/agent.py`, `pi/runtime.py`:* port verbatim from v6 source (clean license-compatible move). Module-level rename: drop `pi_python_harness` package, use `libharness.pi`. Adjust imports.
- *`pi/server.py`:* keep current code, plus the v6 `tool_executor` constructor arg + `_execute_tool_blocking` path. Both paths stay (executor-using and direct-loop). Current bridge protocol unchanged.
- *`pi/tools.py`:* keep `MANIFEST_PROTOCOL_VERSION` and current schema-inference code; add v6's `_lock` + `freeze()` + `frozen` + post-freeze rejection. Keep `async def update` + all four handler shapes.
- *`pi/rpc.py`:* near-verbatim from current (preserves `set_model` fix and `pi-mono` field-name comment). No structural change.
- *`pi/harness.py`:* keep the existing `PiPythonHarness`. Document it as the lower-level async power-user surface; `PiAgentHarness` becomes the recommended embedding surface (matching v6's stance).
- *`tests/pi/test_threaded_agent.py`:* port v6's thread-affinity, dedicated-loop, multi-harness, shared-pool tests. Adjust imports.
- *`docs/DESIGN.md`:* swap the architecture sketch to show `HarnessRuntime` + owner-thread proxies + tool pool. Resolved-decisions table gets a new 2026-05-17 row and the 2026-05-15 row marked superseded.
- *`SESSION-STATE.md`:* current state describes v6-port-in-progress; pending tasks reshuffle (Agent class shape becomes the main co-design instead of "Agent class blocks the threads rewrite").
- *`2026-05-15-threads-rewrite-plan.md`:* frontmatter status → "Superseded". Leave the body intact for historical reference.
- *`2026-05-15-concurrency-model-discussion.md`:* the existing Resolution block becomes "Initial resolution (2026-05-15)" + a new "Revised resolution (2026-05-17)" block. Body unchanged.
- *`2026-05-17-agent-class-design.md`:* new co-design doc specifying the `Agent` shape under v6 — namespaces (`Agent` vs `PiAgentHarness` overlap), the `on_*` hook set, dispatch mechanism (H3 default + H2 opt-in), how hooks subscribe via the proxy.
- *Makefile:* judgment call — keep 3.11+3.14t in `make all` or demote 3.14t. Recommendation above: demote to opt-in.

## Decision register (what we'd be committing to)

| Decision                            | Picked                                                |
| ----------------------------------- | ----------------------------------------------------- |
| Concurrency model                   | Option B (asyncio core in dedicated thread)           |
| `PiAgentHarness` retention          | Keep — recommended surface                            |
| `PiPythonHarness` retention         | Keep — async-first power-user surface                 |
| Multi-harness support               | First-class (one `HarnessRuntime`, many harnesses)    |
| Tool handler shapes accepted        | sync def, async def, sync-gen, async-gen              |
| Streaming primitives accepted       | yields + `ctx.update` (both)                          |
| Hook dispatch (default)             | H3 — sync `on_*`, blocking the loop thread            |
| Hook dispatch (opt-in)              | H2 — sync `on_*`, via `run_in_executor`               |
| `async def` rejection at decoration | No — async tools accepted                             |
| Cancellation primitive              | `threading.Event` (v6 already does this)              |
| Registry lifecycle                  | `freeze()` before `start()`                           |
| FT (3.14t) verification             | Opt-in via `make all-ft`; not default                 |
| Python floor                        | 3.11 (matches current; v6 says 3.10 but we want 3.11) |
| `set_model` wire shape              | `modelId` (preserve current fix)                      |
| Manifest `protocolVersion`          | Required, asserted in shim (preserve current)         |

## What the spark work becomes

Practical question that the doc shouldn't dodge: **what happens to the in-progress threading rewrite on spark?**

Three options:

1. **Discard.** The work was useful as a forcing function for this discussion. Lessons learned go into the v6 adoption plan; code goes away.
1. **Archive as a predecessor.** Vendor the spark branch as `dev-notes/predecessors/v7-spark-threads/` (or similar), in the same shape as v1–v5. Preserves the evidence of "we tried, here's what we learned."
1. **Keep alive on a branch.** Don't delete; don't merge; revisit if v6 turns out to have problems we didn't predict.

Recommendation depends on author judgment about how complete the spark work is and whether any of it (e.g. test improvements, regression-test ideas from § 10 of the rewrite plan) are valuable independent of the threading direction. Some likely candidates for harvest:

- The 10 concrete regression tests in the threads-rewrite plan § 10 — most apply equally to v6 (handler leak on mid-stream disconnect, SIGKILL external, concurrent execute, etc.). Port the test cases without porting the threading implementation.
- Documentation/clarification about pi's subprocess-buffering behavior, if it was learned during spark work.

## Roadmap

Sequenced phases, each landable independently.

### Phase A — Vendor v6 + write the design doc

1. Copy `dev-notes/predecessors/v6-threaded/src/pi_python_harness/{agent,runtime,server,tools}.py` into `src/libharness/pi/` (with import-path adjustments).
1. Re-apply the two current improvements: `set_model` → `modelId`; `MANIFEST_PROTOCOL_VERSION`.
1. Port `tests/test_threaded_agent.py` → `tests/pi/test_threaded_agent.py`.
1. `make all` on both venvs must stay green.
1. **Don't** delete `PiPythonHarness` yet — both APIs coexist during the transition.
1. Update `docs/DESIGN.md` § Resolved decisions and architecture sketch.

### Phase B — Agent-class co-design

Co-design with the author:

- `class Agent` shape: subclass-hook style (Template Method) vs callback-registration style.
- Hook set: enumerate the `on_*` methods we expose. Notification-only vs decision events.
- Dispatch mechanism: confirm H3 default + H2 opt-in.
- Relationship between `Agent` and `PiAgentHarness`: composition (agent holds a harness) vs subclassing (agent extends harness).

Output: `dev-notes/2026-05-17-agent-class-design.md`.

### Phase C — Implement Agent + integrate hooks

After Phase B converges:

1. Implement `Agent` base class in `src/libharness/pi/agent.py` (or a new `src/libharness/pi/agent_class.py` to avoid name collision with v6's `PiAgentHarness` module).
1. Wire hook dispatch into `PiRpcClient._dispatch_event`.
1. Tests: `tests/pi/test_agent_hooks.py` covering hook ordering, exception handling, the deadlock footgun (assertion that calling `harness.client.prompt()` from a hook deadlocks predictably with a clear error).
1. Update README example to show the `Agent`-with-`on_*` shape.

### Phase D — Event bridge (the original co-design)

Once hooks are real, revisit `dev-notes/2026-05-14-event-bridge-proposal.md`. Design now happens against the v6 architecture: events are pi-side events, hooks are Python-side reactions, dispatch is via the loop thread → hook executor.

## Open questions for the author

1. **Spark experience details.** What specifically prompted the doubt? Concrete pain points would shape the v6 plan — e.g. if the threading rewrite was failing because of resource-cleanup gotchas, v6 inherits the (more mature) asyncio cleanup story. If it was failing because of test ergonomics, v6 likely doesn't fix that (asyncio tests are not obviously easier than threading tests).
1. **`PiAgentHarness` and `Agent` overlap.** v6 uses `PiAgentHarness` for the thread-owned proxy/facade. The author's design intent for `Agent` is "the class with `on_*` hooks." Are these the same class? Composition? Inheritance? The naming will need to be settled early.
1. **Async tool retention.** Adopting v6 means continuing to accept `async def` tool handlers. Is that OK, or do we want v6 + a sync-only registration policy? (The latter is doable — keep v6's internals async but reject async-def at decoration. Surprising but coherent.)
1. **FT-first status.** Drop FT-first as a goal, or keep it as a smaller-scope goal ("FT speeds up parallel tools")?
1. **Disposition of the spark branch.** Discard, archive as a predecessor, or keep on a branch?

## Risks of adopting v6

Honest risk inventory:

1. **The decision was *just* revisited.** Reversing a recorded decision two days after making it has organizational cost (whoever reads the doc trail has to understand both decisions). Mitigation: write the supersession clearly; don't pretend the original decision was wrong, just that new information (the spark experience) shifted the balance.
1. **v6 hasn't been validated with real pi.** v6's `RUN_RESULTS.md` says "this rerun did not reinstall the Pi npm package in the sandbox" for the threaded reshape pass. The real-pi integration test was only validated against the pre-threading synthesis. We'd be relying on v6's threading layer being correct on top of a known-working core; integration testing under libharness's sandbox is needed before we trust the port.
1. **Two harness classes is friction.** Keeping both `PiPythonHarness` (async) and `PiAgentHarness` (sync facade) is what v6 does, but it's documentation overhead. Users need to know which to pick. Mitigation: clear "use `PiAgentHarness` unless you have a specific reason" guidance, eventual deprecation of the async surface if nobody uses it.
1. **The `on_*` hook dispatch (H3) shares the loop blocking risk.** A slow hook freezes I/O. This is the same risk the threads-rewrite plan has for its reader thread, but under v6 it's worse if the loop also has bridge connections to serve — a slow hook could timeout pi's bridge call as well. Need to test this and document the constraint.
1. **`tool_executor` interaction with `ctx.update`.** v6's `send_update_threadsafe` calls `asyncio.run_coroutine_threadsafe(...)`. If the loop is blocked (e.g. by a slow hook), the tool's `ctx.update` blocks too. This is a transitive dependency that wasn't in v5. Worth a regression test.
1. **Async-first `PiPythonHarness` rots.** If `PiAgentHarness` is the recommended surface, the async one probably gets less attention and breaks subtly over time. Mitigation: keep at least one test that uses it directly (v6 carries the v5 integration test which does).

## My read

The case for v6 is real and worth taking seriously. The H3 dispatch insight (sync hooks via a sync handler called from `async def _dispatch_event`) addresses what was the load-bearing objection to option B in the original concurrency discussion. The code-preservation argument and the multi-harness ergonomics are independent pros that the rewrite plan doesn't match.

The argument *against* shifting is mostly: "we just decided, two days ago." That's not a strong technical argument; it's an organizational one. The technical case for the threading rewrite was strong but contingent on (a) FT-first being a primary goal and (b) sync hooks being unreachable from async internals. Both of those are now softer than they were on 2026-05-15.

I'd lean toward v6 *if* the author's spark experience confirms that the threading rewrite is harder than expected. If the spark work is going fine and the doubt is more like "I'm second-guessing," then sticking with the rewrite plan is also defensible.

Either way: the next step is talking through the spark experience before making the call. This doc is the option-B detail; the decision rests on what specifically broke under option D.
