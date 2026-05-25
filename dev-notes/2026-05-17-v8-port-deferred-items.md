---
status: Active
created: '2026-05-17'
---

# v8 port — deferred items, with justifications and recommendations

Single rollup of everything that was **deliberately not done** during the v8 port sprint. Use this doc when you want to answer "should I do X?" or "why didn't we do Y?" without having to scan the synthesis doc, analysis doc, port plan, and journal separately.

Each entry has: where it was originally deferred, what implementing it would change, *why* the deferral was made, my **recommendation** (fix now vs remain deferred) given the user's stated bias toward not deferring, and the trigger condition for revisiting if it stays deferred.

## Recommendation rubric

The user's stated bias is to NOT defer. So:

- **Fix now** = my default unless I have a strong defer argument.
- **Remain deferred** = I have a *strong* position that the defer is correct (not just inertia). Each "remain deferred" carries a 2-3 sentence justification.

Use the recommendation summary at the bottom for a quick read; the per-item detail in the body has the reasoning.

## Scope and audience

**Audience:** future sessions picking up this branch; reviewers asking "what didn't we do?"; the next sprint's planner deciding where to start.

**In scope for this doc:**

- Items the v8 port left undone on purpose.
- Items downgraded or rescheduled during the port.
- Items the post-port ergonomic pass should consider.

**Out of scope for this doc:**

- Anything that was done (read `dev-notes/2026-05-17-v8-port-journal.md` for the chronological audit trail; `git log --oneline main..HEAD` for the commit-level record).
- Future net-new features that weren't even considered during the port (those belong in a roadmap doc, not a deferred-items doc).

## Source documents (cross-references)

| Source                                                         | Coverage                                          |
| -------------------------------------------------------------- | ------------------------------------------------- |
| `dev-notes/2026-05-17-v8-analysis.md` § Items worth discussing | Items A, B, D, F (review-of-review findings)      |
| `dev-notes/2026-05-17-v8-analysis.md` § Resolutions            | Author resolutions (e.g., `_decision_timeout_ms`) |
| `dev-notes/2026-05-17-v8-port-plan.md` § Out of scope          | Architectural out-of-scope items                  |
| `dev-notes/2026-05-17-v8-port-review-synthesis.md` § Tier 2    | Gate A.5 findings F8-F36 (22 items)               |
| `dev-notes/2026-05-17-v8-port-journal.md`                      | Phase 6 Item-E deferral; F20 fold-in note; etc.   |
| `dev-notes/SESSION-STATE.md` § Pending tasks                   | Post-port pointer for the next session            |

## Categories

| Letter | Category                          | Count | Where they came from                        |
| ------ | --------------------------------- | ----- | ------------------------------------------- |
| A      | Author-resolution deferrals       | 6     | Analysis § Resolutions (2026-05-17)         |
| B      | Gate A.5 Tier-2 review findings   | 22    | Synthesis doc § Tier 2 (F8-F36)             |
| C      | In-flight deferrals (during port) | 2     | Phase 6 (Item E); Phase 5.5 sibling fold-in |
| D      | Architectural out-of-scope        | 5     | Port plan § Out of scope                    |
| E      | Longstanding backlog (pre-port)   | 2     | SESSION-STATE (carried over)                |
| F      | Future roadmap                    | 3     | DESIGN.md § Roadmap                         |

Total: ~40 entries with significant overlap between categories (Item B = F27 = D.4; Item A = F26 = D.5; Item D = F10). The detail sections below de-duplicate.

## A. Author-resolution deferrals

Decisions the author made during the design-review phase that say "yes, this is a real concern, but we're not addressing it in v1 of the port."

### A.1: Item A — decision-hook exception is logged twice

**Source:** analysis § Items worth discussing A. **Synthesis Tier-2:** F26.

**What:** decision hook raises → logged once via `_LOG.exception` in `_dispatch_decision_hook`; the exception re-raises into `_async_on_bridge_event`; `server.py`'s `_dispatch_bridge_event` catches and logs again before returning the failure response to the shim.

**Why deferred:** cosmetic. The fail-open behavior is correct (pi proceeds as if no decision was made); the duplicate log is mildly noisy but useful for debugging in different layers. No silent-failure or correctness concern.

**Status: DONE (ergonomic-pass batch 1).** Inner `_dispatch_decision_hook` catch in `agent_class.py` sets `exc._libharness_logged = True` before re-raising; outer `server.py:_dispatch_bridge_event` catch checks the sentinel and skips the duplicate log. Regression test in `tests/pi/test_ergonomic_pass_cluster1.py::test_decision_hook_exception_logged_once` pins the one-log behavior via caplog.

### A.2: Item B — shim-side `DECISION_EVENTS` array not validated against pi source

**Source:** analysis § Items worth discussing B. **Synthesis Tier-2:** F27.

**What:** `src/libharness/pi/shim.py` hardcodes 19 decision event names in a TypeScript array. The Python-side `test_pi_event_taxonomy.py` parses pi-mono source and asserts the Python frozenset matches, but the TS array gets no such check.

**Why deferred:** drift is detectable at the Python layer (taxonomy test catches it); the TS-side check is a hardening pass. A new pi event wouldn't cause silent corruption — it would simply not fire any hook.

**Status: DONE (ergonomic-pass batch 3).** Delegated to an Opus subagent. The TS shim's `DECISION_EVENTS` array is now generated from `AgentHookSurface._DECISION_EVENT_NAMES` at `write_bridge_shim` time via a new `__DECISION_EVENTS__` placeholder. Sorted output ensures deterministic shim generation; the new `test_generated_shim_decision_events_matches_python_source` test pins the invariant. Single source of truth eliminates drift.

### A.3: Item D — `_handler_wants_context` heuristic edge cases

**Source:** analysis § Items worth discussing D. **Synthesis Tier-2:** F10.

**What:** the dispatcher inspects the hook signature to decide whether to pass `ctx`. The canonical signatures (`(self, event)` or `(self, event, ctx)`) work. Edge case: `def decide_X(self, *, ctx)` triggers the heuristic but then `_call_decision_handler` invokes positionally → `TypeError: handler() takes 2 positional arguments but 3 were given`.

**Why deferred:** the edge case errors loudly (not silently) and the fix on the user side is a one-line signature change.

**Status: DONE (ergonomic-pass batch 2).** Refactored `_handler_wants_context` into `_handler_ctx_mode` that returns `(wants_context, kw_name)`. The dispatcher now passes ctx via `**{kw_name: ctx}` when the heuristic detects a keyword-only parameter. Regression test in `tests/pi/test_ergonomic_pass_cluster2.py` exercises both the detection and the dispatch.

### A.4: Item F — wire frame field names diverged from prompt sketch

**Source:** analysis § Items worth discussing F.

**What:** v8 uses `{"event": "...", "data": {...}}` for bridge frames. The analysis prompt sketched `{"event_name": "...", "event_data": {...}}`.

**Why deferred:** purely cosmetic. Pi never sees these frames — they're internal Python ↔ TS bridge envelopes. The names are consistent on both sides of the bridge.

**Status: Remain deferred (confirmed).** This is genuinely cosmetic — the names work, are consistent across both sides, and renaming would churn the protocol with zero gain. There's no audience that benefits from `event_name` over `event`: pi doesn't see them, users never write them, the docs match the implementation. The original analysis-prompt convention was a draft, not a requirement. Defer is correct; revisit only if a different audience materializes (e.g., third-party shim authors who'd find verbose names easier to skim).

### A.5: Default `_decision_timeout_ms = 30000` rejected (HITL case)

**Source:** analysis § Resolutions (recommendations doc proposed 30s; rejected). **Port plan:** § Out of scope.

**What:** the recommendations doc proposed defaulting `_decision_timeout_ms` to 30s "so a buggy `decide_X` can't wedge pi indefinitely." Rejected: the default stays `None` (no timeout).

**Why deferred:** human-in-the-loop decision hooks may legitimately need to block for hours or days. Assuming non-HITL by default would bake in a wrong assumption.

**Recommendation: Remain deferred (= remain rejected).** This is the user's own explicit resolution from 2026-05-17 and it's load-bearing: the library's HITL use case is a primary design driver. Setting a default would either (a) constrain HITL users to bypass it (back to None) or (b) silently kill long-running HITL hooks. The cost of defaulting is high (wrong-by-default for the load-bearing use case) and the protection is opt-out trivially. Strong position: don't touch the default.

### A.6: Runtime opt-in for decision events

**Source:** analysis § Resolutions (deferred). **Port plan:** § Out of scope.

**What:** `agent.enable_decision_for(...)` / `agent.disable_decision_for(...)` to flip gates after extension load. Currently, the gate set is fixed at extension load; method existence is the opt-in trigger.

**Why deferred:** the method-existence trigger covers the canonical use case. Runtime opt-in adds per-Agent gate state, bridge round-trip for flips, and a race window during transition.

**Recommendation: Remain deferred.** The method-existence trigger is *sufficient* for every known use case. Adding runtime opt-in costs: new bridge call types, gate-state synchronization between Python and TS, a race window during gate transitions, and the complexity of "what happens to in-flight events when the gate flips mid-fire?" That's substantial engineering with zero current motivating case. Strong position: do nothing until a real use case emerges (e.g., "decision hook should be active only during the first N turns" or similar). The canonical workaround — define a separate `Agent` subclass per gate set — works.

## B. Gate A.5 Tier-2 review findings

This category is detailed in `dev-notes/2026-05-17-v8-port-review-synthesis.md` § Tier 2 with reviewer agreement counts and full per-item context. The table below is the rollup with **my recommendation** added; the **Detail** list below the table justifies the few "Remain deferred" calls.

| ID  | Sev  | Title                                                                  | Rec   |
| --- | ---- | ---------------------------------------------------------------------- | ----- |
| F8  | HIGH | Shared `hook_executor` max_workers=1 serializes HITL across harnesses  | Done  |
| F9  | HIGH | `_call`/`submit` race with `close()` — queued commands hang            | Done  |
| F10 | MOD  | Keyword-only `ctx` dispatched positionally (= Item D)                  | Done  |
| F11 | MOD  | `_decision_timeouts_ms` mutable class-default footgun                  | Done  |
| F12 | MOD  | `Agent.__init__` silently overwrites three user-provided kwargs        | Done  |
| F13 | MOD  | `watch_disconnect` sets `cancelled` in finally on normal completion    | Done  |
| F14 | MOD  | Reader-task death doesn't terminate pi subprocess                      | Done  |
| F15 | MOD  | `close()` hangs if reader awaits handler that swallows CancelledError  | Done  |
| F16 | MOD  | `ProcessLookupError` during SIGTERM aborts `close()` cleanup           | Done  |
| F17 | MOD  | `Agent` not exercised in the 13-test suite                             | Done  |
| F19 | MIN  | `_decision_timeouts_for_manifest` advertises timeouts for closed gates | Done  |
| F20 | MIN  | `context or {}` falsy coercion                                         | Done  |
| F21 | MIN  | `AgentHookSurface` subclasses can bypass validation                    | Defer |
| F22 | MIN  | Sync notification hooks returning awaitables: silently dropped         | Done  |
| F23 | MIN  | Timeout validator accepts non-int values (`True`, `0.5`)               | Done  |
| F24 | MIN  | Failed `start()` leaves `self.process` set, blocking retry             | Done  |
| F25 | MIN  | Constructor-passed `decision_timeouts_ms` skips validation             | Done  |
| F26 | MIN  | Decision-hook exception logged twice (= Item A)                        | Done  |
| F27 | MIN  | Shim-side `_DECISION_EVENT_NAMES` drift (= Item B)                     | Done  |
| F28 | MIN  | `cast(object, Agent)` workaround needs comment                         | Done  |
| F29 | MIN  | `PiLaunchConfig.startup_timeout` misnamed (sleeps ≤0.2s)               | Done  |
| F30 | MIN  | Test asserts state but not order of cleanup                            | Defer |
| F31 | MIN  | Consistency-check helper only runs on the base                         | Defer |
| F32 | MIN  | `_check_owner` error message missing harness_id                        | Done  |
| F33 | MIN  | `next_event()`/`wait_for_event()` waiters never unblocked on `close()` | Done  |
| F34 | MIN  | Caller-supplied duplicate request IDs can overwrite `_pending`         | Done  |
| F35 | MIN  | Tool params named `context`/`ctx` get silently hijacked                | Done  |
| F36 | MIN  | Same key-precedence footgun in `shim.py`'s `bridgeCall`/`bridgeNotify` | Done  |

**Detail (Tier-2 items I recommend keeping deferred):**

- *F14 — Done (2026-05-25).* Resolved by **two layered fixes**, not by the synthesis doc's "conservative vs aggressive kill" dichotomy (which turned out to be a false dichotomy). (1) `StrictJsonlDecoder.feed()` returns `(records, errors)` instead of raising on the first malformed record; per-record decode failures are surfaced loudly (logger.error + `print(..., file=sys.stderr, flush=True)` belt-and-braces + tracked on the client as `_jsonl_decode_error_count` / `_first_jsonl_decode_error` / `_last_jsonl_decode_error`) so a pi-side bug doesn't get silently swallowed; the decoder's state advances past the bad record so subsequent reads still work. (2) For the residual catastrophic cases where the reader does die (pipe broken, buffer overflow, framework bug), the exception is stashed in `_reader_failure`; `send()` checks this and fails fast with a clear `PiRpcProcessError` instead of writing to pi's stdin and timing out 30s later. Net effect: the reader stays alive across realistic transient pi bugs (visibility preserved via log + stderr + counter), and the catastrophic cases produce immediate caller-visible errors. Regression tests: `tests/pi/test_jsonl.py` (5 cases for the decoder) + `tests/pi/test_f14_reader_death.py` (4 cases for the client-side counters + fail-fast). Also fixed `server.py:_serve_client`'s `decoder.feed()` call to handle the new tuple return.
- *F8 — Done (2026-05-24).* Resolved by folding sync-hook dispatch into the owner thread, **not** by changing executor sharing scope. `hook_executor` removed from `HarnessRuntime` entirely. Sync hooks now enqueue `_HookCall` messages on the harness command queue; the owner thread (`PiAgentHarness-<id>`) consumes them naturally serialized FIFO with the harness's other operations. Operations that touch the loop (`call_rpc`, `start`, `close`, `(un)subscribe_client_events`) return `concurrent.futures.Future`; the owner-thread dispatcher registers a `_Continuation` callback and stays free to handle hook calls during long-running RPCs. Cross-harness HITL serialization is structurally impossible — each harness has its own queue and its own consumer. Regression: existing `test_threaded_agent.py` + the rewritten `test_sync_hook_fires_on_owner_thread_not_loop` and `test_concurrent_decision_events_are_serialized_by_owner_thread` pin the new invariants. **Follow-up gap:** `threaded=False` mode still uses `asyncio`'s default executor for sync hooks (MainThread blocked on `Future.result()` can't pump the queue). Tracked as a new entry in § C and in `dev-notes/2026-05-24-level-3-runtime-audit.md`.
- *F17 — Done.* The synthesis doc was written against the pre-Phase-6 state (13 tests). After Phase 6, `tests/pi/test_agent_class.py` has 16 tests against Agent (including 3 strict-mode E2E tests for decision events from Item C), and `test_decision_hooks.py` / `test_channel_separation.py` exercise the surface further. The 56-test suite resolves the bulk of this finding. Any remaining gaps are absorbed by the future tests for the other fixes.
- *F20 — Done.* Already folded into commit `4d0bee1` (Tier-1 fixes) per the synthesizer's note. Listed in the table for completeness; no action needed.
- *F21 — Remain deferred (risk now documented in docstring).* Subclassing `AgentHookSurface` directly (without going through `Agent`) is not a documented or supported user pattern. Adding the `__init_subclass__` validation to `AgentHookSurface` changes the invariant about who validates whose surface and could surprise the (currently zero) users who subclass the mixin to compose differently. Defer until a real use case materializes. As of ergonomic-pass batch 1: the `AgentHookSurface` class docstring now explicitly documents the risk and points at this entry, so a future direct-subclasser sees the warning before they trip the gap.
- *F30 — Remain deferred.* The existing cherry-pick test asserts the post-cleanup state (task slots null, process reaped). Strengthening it to assert the *order* of cleanup operations would require instrumenting the helper with a counter or monkeypatching the asyncio.gather call. The test surface lives in the right spirit; adding fragility for theoretical order-bug protection isn't worth it. Defer until someone actually changes the cleanup order and breaks the spirit.
- *F31 — Remain deferred.* The consistency-check helper guards the *library's* declarative surface. Running it on every user subclass adds runtime cost on every Agent subclass instantiation. User subclasses that drift from the library surface fail validation differently (the existing `_validate_declared_hook_names` covers user-side typos in hook names). Library-side drift is what matters; library-side drift is what the helper catches. Defer indefinitely.

All other Tier-2 items are recommended Fix-now. Quick aggregation of fix-effort estimates:

- Trivial (≤5 LOC, 1 test): F11, F19, F24, F28, F32, F34, F36 (7 items).
- Small (5-20 LOC, 1-2 tests): F10, F12, F13, F15, F16, F22, F23, F25, F26, F29, F33, F35 (12 items).
- Medium (design call + 20-50 LOC): F8, F9, F14, F27 (4 items).

Total fix-now Tier-2 effort: ~25 items × small-to-medium = a focused ergonomic-pass commit of ~300-500 LOC plus regression tests. Doable in one focused sprint session.

## C. In-flight deferrals (during port)

### C.1: Item E — `subscribe_client_events` thread-bounce simplification

**Source:** analysis § Items worth discussing E (planned for port). **Journal:** Phase 6 entry ("Item E deferred").

**What:** the install path is owner-thread → loop-thread → `list.append`. The simplification is to schedule directly on the loop thread, skipping the owner-thread bounce.

**Why deferred during Phase 6:** the advisor doc rated it "low-risk in isolation; low value too." Without deeper analysis of the loop/owner ownership model for the handler list, the safe call was to defer.

**Recommendation: Remain deferred.** The benefit is *one fewer thread hop* during `agent.start()`/`agent.close()` — once per lifetime, not a hot path. The risk is non-zero: the install/uninstall path has implicit ordering with the owner thread, and bypassing it could race with other commands that *are* owner-thread-ordered. Cost is small (a few lines); value is smaller still. This is the textbook case where defer is correct: a "cleanup" with no measurable benefit and a non-zero invariant risk. Defer to the F8-driven threading-model audit; if that audit reshapes the runtime-level ownership, this becomes free; if it doesn't, leave it alone.

### C.2: F20 — addressed already

Folded into the Gate A.5 Tier-1 fix commit per synthesizer's note. Listed here for the audit trail; not an open deferral.

## D. Architectural out-of-scope (from the port plan)

### D.1: Per-event TypedDicts for decision-hook return shapes

**Source:** port plan § Out of scope.

**What:** instead of returning a raw `dict`, decision hooks would return a `TypedDict` per event.

**Why deferred:** 19 new public TypedDict classes; per-event return-shape documentation already covers user needs; pi-mono shape changes would require regen.

**Recommendation: Remain deferred.** This is a *future-feature deferral*, not a port-time skip. The doc contract works; users get autocomplete on event payloads via the existing `AgentEvent` mapping protocol. TypedDicts add real maintenance cost — every time pi-mono changes a shape, every TypedDict needs to track it. The library would become a bigger surface for marginal compile-time benefit. Strong position: defer until a user surfaces a shape mismatch that compile-time checks would have caught. Until then, runtime + doc is the right contract.

### D.2: Pi-native session method wrappers

**Source:** port plan § Out of scope. **SESSION-STATE:** Pending tasks.

**What:** `fork`, `clone`, `switch_session`, `get_session_stats`, `export_html`, `set_session_name`, `get_fork_messages` as typed methods on `PiRpcClient`. ~100-150 LOC.

**Why deferred:** independent of v8 port; predates it.

**Status: DONE (ergonomic-pass batch 1).** Delegated to an Opus subagent that verified pi's actual RPC contract in pi-mono's `rpc-types.ts` + `rpc-mode.ts` and used pi's narrower argument shapes (e.g., `fork(entry_id: str)` operating on the current session — pi doesn't expose a `parent_session_id` parameter). 7 typed methods added; 15 tests in `tests/pi/test_session_wrappers.py` pin the wire shape via the same `data.received: request` echo pattern the existing `set_model` test uses.

### D.3: `PiPythonHarness` deprecation

**Source:** port plan § Out of scope (per author resolution #3).

**What:** add a `DeprecationWarning` to `PiPythonHarness.__init__` and eventually remove.

**Why deferred:** author resolution #3 keeps it for v1 of the port.

**Recommendation: Remain deferred.** Author resolution #3 is explicit. Adding a `DeprecationWarning` now would partially-violate the resolution ("keep for v1" implies "don't actively migrate users yet"). The right time to deprecate is after the v8 surface has been used in real codebases for long enough that the migration path is well-understood — which is precisely what "v1 of the port" means. Strong position: respect the resolution; revisit after one or two production deployments report back.

### D.4: Shim-side `DECISION_EVENTS` drift detection (= A.2 / F27)

Covered in A.2. Recommendation: Fix now.

### D.5: Decision hook exception logging dedup (= A.1 / F26)

Covered in A.1. Recommendation: Fix now.

## E. Longstanding backlog (pre-port)

### E.1: Generator-through-bridge regression test

**Source:** SESSION-STATE (carried from earlier sessions).

**What:** sync-generator handlers yielding multiple `update` frames are supported in `tools.py:collect_tool_result` but never end-to-end tested through the bridge.

**Why deferred:** the test gap is real but v8 preserves the path; the gap survived the v8 port.

**Status: DONE (ergonomic-pass batch 1).** Delegated to an Opus subagent. `tests/pi/test_generator_through_bridge.py` covers both the sync-generator path (`inspect.isgenerator` branch in `collect_tool_result`) and the async-generator path (`inspect.isasyncgen`). Subagent's finding: the generator path is intact under v8; no regression. Each test goes through the real `PythonToolServer` loopback TCP wire, asserts N ordered `update` frames followed by exactly one `response` frame with matching `id`. **Behavioral note logged during the test write:** the `for item in value` loop in `collect_tool_result` discards a generator's `return X` (Python `StopIteration(X)` swallow); the *last yielded value* becomes both the final `update` and the `response.data` — the tests assert this actual behavior, not the assumed-but-wrong "yields + return" model.

### E.2: Generator-through-bridge feature

The *feature* is supported; only the *test* is missing. Not a real deferral. Folded into E.1.

## F. Future roadmap (post-port)

### F.1: Command bridge

**Source:** DESIGN.md § Roadmap.

**What:** let Python register pi slash commands (`/something`) the same way TypeScript extensions can.

**Why deferred:** no user case yet; v8 focused on the event/decision bridge.

**Recommendation: Remain deferred.** This is a forward-looking feature, not a port-related deferral. There's no user case yet, no concrete protocol, no design doc. Building it now would be hypothetical engineering. Defer per the standard rule: build when a real user case lights up the need. The bridge protocol supports it; the runway is there.

### F.2: State bridge

**Source:** DESIGN.md § Roadmap.

**What:** let Python query and mutate session state mid-run.

**Why deferred:** mutation timing is the hard part; needs design work.

**Recommendation: Remain deferred.** Same reasoning as F.1. Forward-looking; no user case; mutation-timing design needs a real motivating scenario to be sized correctly. Premature design without a use case tends to produce the wrong abstraction.

### F.3: UI bridge

**Source:** DESIGN.md § Roadmap.

**What:** richer UI bridge for `select`/`input`/`confirm`/`editor` dialogs.

**Why deferred:** the headless auto-handler covers the test-environment use case; interactive UI requires a host-side implementation.

**Recommendation: Remain deferred.** Same family as F.1/F.2. The headless implementation works for the only current consumer (tests); interactive UI requires a host UI to render *through*, which doesn't exist yet. Build when a host UI demands it.

## Recommendations summary

| Recommendation                        | Count |
| ------------------------------------- | ----- |
| **Fix now** — need the council        | 0     |
| **Remain deferred** — strong position | 10    |
| **Done**                              | 29    |

**Detail (which items go in each bucket):**

- *Fix now (0):* all priority items resolved.
- *Remain deferred (10):* A.4 (wire-frame names), A.5 (`_decision_timeout_ms` default), A.6 (runtime opt-in), F21 (subclass validation bypass — risk now documented in `AgentHookSurface` docstring), F30 (cleanup-order test), F31 (subclass-side consistency check), C.1 (Item E thread-bounce), D.1 (TypedDicts), D.3 (`PiPythonHarness` deprecation), F.1 / F.2 / F.3 (roadmap bridges — count as one entry; same family).
- *Done (29):* F17 (Agent test coverage — Phase 6), F20 (folded into commit `4d0bee1`); batch 1 commit `02cc67a` — A.1 / F26, F28, F29, F32, F35, D.2, E.1; batch 2 commit `d90d626` — A.3 / F10, F11, F12, F22, F23, F25; batch 3 commit `13dd691` — A.2 / F27, F19, F24, F33, F34, F36; batch 4a — F9, F13, F15, F16; **F8 (2026-05-24) — fold-hooks-into-owner-thread; see § B detail above**; **C.3 (2026-05-25) — MainThread-as-pump in threaded=False; see § C detail above**; **F14 (2026-05-25) — reader stays alive across per-record decode errors (logged + counter); catastrophic crashes fail-fast on next send; see § B.F14 detail above**.

### C.3: `threaded=False` design debt — MainThread is now the queue consumer (DONE 2026-05-25)

**Surfaced by:** F8 implementation. The fold-hooks-into-owner-thread design relies on a thread consuming the command queue. In `threaded=True` that's the dedicated owner thread (`PiAgentHarness-<id>`). In `threaded=False` it should be MainThread — MainThread is conceptually the owner thread when no separate thread is spun up.

**Current behavior:** MainThread is blocked on `concurrent.futures.Future.result()` while a harness operation is in flight. It does not pump the command queue. So `_dispatch_sync_hook` falls back to `loop.run_in_executor(None, fn)` — asyncio's default executor — which (a) is a different code path from `threaded=True`, (b) has multiple workers so no FIFO guarantee across concurrent hooks, (c) doesn't share thread affinity with the harness state.

**Design invariant we want:** *one* code path for sync hook dispatch. The dispatcher consumer differs only in *who* — dedicated owner thread (`threaded=True`) or MainThread (`threaded=False`). No `if self.threaded:` branch in `_dispatch_sync_hook`.

**What blocks the clean fix:**

1. Harness public methods (`harness.prompt_and_wait`, etc.) would need to enter a pump loop when `threaded=False` — block on the command queue, dispatching `_HookCall` / `_Continuation` until our own operation's `main_future` resolves. Doable.
1. White-box tests bypass the harness API (`runtime.run_async(agent._async_on_event(...))`, raw bridge-socket via `_event_request`). In these patterns MainThread is blocked on something that isn't pump-aware (raw `Future.result()` or `socket.recv()`). Would deadlock if hooks tried to dispatch to MainThread. Restructuring these tests to use harness API is meaningful work.
1. Or: deprecate `threaded=False` entirely — force all tests onto `threaded=True`. Cleanest end state; biggest refactor.

**Status: DONE (Level 3 Option A, 2026-05-25).** `_call` pumps the harness command queue while waiting for its operation in `threaded=False`; `pump_until(target)` lets tests that bypass the harness public API pump explicitly (target can be a coroutine, a `concurrent.futures.Future`, or a `threading.Event`). The `if self.threaded:` branch in `_dispatch_sync_hook` is removed — single dispatch code path; consumer is either the dedicated owner thread (`threaded=True`) or MainThread (`threaded=False`). Test churn was contained to a centralized `_event_request` refactor + a small batch of `runtime.run_async → agent.pump_until` call-site swaps. Known limitation documented in `docs/AGENT_HOOKS.md`: blocking sync hooks under `threaded=False` block MainThread; tests that need cancellation-during-blocking-hook should use `threaded=True`.

26 fix-now items + the carrying of A.5 (HITL no-default-timeout, already resolved) and A.4 (cosmetic wire-frame names). The fix-now set clusters cleanly into:

- **Threading-model audit** (F8, F9, F13, F14, F15, F16, C.1 if folded in): 1 design-call commit + ~6 cleanup commits.
- **Validation tightening** (A.3 / F10, F11, F12, F22, F23, F25): 1 commit, ~50 LOC + parametrized tests.
- **Diagnostics / docs / cosmetic** (A.1 / F26, F28, F29, F32, F35): 1 commit, ~30 LOC.
- **Bug fixes** (F19, F24, F33, F34, A.2 / F27, F36): 1 commit per logical surface (server, rpc, shim), ~50 LOC each.
- **Independent work** (D.2 session wrappers, E.1 generator test): 1 commit each, ~100-150 LOC.

Total ergonomic-pass effort: roughly 4-6 commits, ~500-700 LOC + tests. Single focused sprint session.

## When to revisit the "Remain deferred" items

| Item                                                | Trigger                                                                             |
| --------------------------------------------------- | ----------------------------------------------------------------------------------- |
| A.4 (wire-frame names)                              | Never (cosmetic; no audience) unless a documentation case demands consistency       |
| A.5 (`_decision_timeout_ms` default)                | Never as a default change; possibly add a non-HITL helper class if patterns warrant |
| A.6 (runtime opt-in for decision events)            | Real use case requiring dynamic gate flipping mid-run                               |
| F21 (`AgentHookSurface` subclass validation bypass) | User reports surprise from direct mixin subclassing                                 |
| F30 (test asserts state not order)                  | Someone changes cleanup order and breaks the spirit                                 |
| F31 (consistency check on subclasses)               | Indefinitely deferred                                                               |
| C.1 (Item E thread-bounce)                          | Fold into F8/F9 threading-model audit                                               |
| D.1 (TypedDicts)                                    | User surfaces a shape mismatch that compile-time would have caught                  |
| D.3 (`PiPythonHarness` deprecation)                 | After 1-2 production deployments report back on the v8 surface                      |
| F.1/F.2/F.3 (roadmap bridges)                       | First user case for each                                                            |

## Adding new deferred items

When the next sprint or ergonomic pass surfaces something else worth deferring, append a new entry to the relevant category. Pattern:

1. Pick the category.
1. Title the entry with what would change if implemented.
1. Cite the source (commit, doc, journal entry).
1. State the *why* explicitly — vague justifications ("seems risky") don't help future readers.
1. State the **Recommendation** (Fix now / Remain deferred / Done) with a one-line justification.
1. State the trigger condition for revisiting if deferred.

Keep this doc as the canonical rollup. If a deferred item later gets done, mark it DONE in place with a pointer to the commit rather than deleting the entry — the audit trail matters.

## Status

**Sprint complete; this doc lives forward.** A future ergonomic-pass session should read this end-to-end before deciding what to tackle. The "Fix now" set is the priority backlog; the "Remain deferred" set carries the justification trail so it isn't relitigated.
