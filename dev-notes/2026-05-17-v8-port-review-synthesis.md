---
status: Active
created: '2026-05-17'
---

# Gate A.5 — adversarial review synthesis

8 reviewers (6 codex gpt-5.5 xhigh: 2 generalist + 4 specialist on hooks/wire/rpc/thread; 2 Opus generalists) ran in parallel against the post-Phase-5.5 state of the v8 port (commit `de0fe82`). This doc consolidates findings, de-duplicates, applies a consistent rubric, and classifies as Tier-1 (must fix before merge) or Tier-2 (defer).

## TL;DR

Zero CRITICAL findings — the three Phase 5.5 cherry-picks held up. Across the 8 reviewers, after de-duplication the unique signal is roughly 20 findings. Seven are Tier-1 (must fix before merge): six are HIGH-severity sibling-bug or silent-correctness issues, one is a HIGH concurrency race with a one-line fix. The dominant patterns are (1) the same `or {}` falsy-coercion shape that Phase 5.5 fixed at one site survives at two more, (2) user-supplied callbacks (UI handler, RPC event subscriber, bridge event handler) can raise and tear down the stdout reader / wedge the client, and (3) `start_owner_thread()` / `AsyncioLoopThread.start()` have lock-drop races that can spawn duplicate threads.

## Tier 1 — must fix before merge

### F1: `event.data` falsy-coercion mirrors the Phase 5.5 `params` bug — same shape, different site

- **Severity:** HIGH

- **Reviewer agreement:** 2 reviewers (codex-spec-wire, opus-gen-1).

- **Location:** `src/libharness/pi/server.py:199`.

- **Observed:** `_dispatch_bridge_event` reads `data = request.get("data") or {}` and then asserts `isinstance(data, dict)`. The `or {}` already coerces `[]`, `0`, `""`, `False` to `{}` before the isinstance check ever runs. This is the exact bug-shape Phase 5.5 cherry-picked at `_dispatch_execute`'s `params` line, but only that one site got fixed.

- **Why wrong:** A buggy or malicious TS-side shim that sends `{"type":"notify_event","event":"tool_call","data":[]}` silently dispatches to the user's `on_tool_call` handler with an empty payload (`{"type":"tool_call"}`) instead of returning `event.data must be an object`. Opus-gen-1 reproduced it live: handler was called with `[({'type': 'tool_call'}, False)]`. Silent correctness bug — every named-event hook on the bridge side is exposed.

- **Suggested fix:** Apply the Phase 5.5 shape:

  ```python
  data = request.get("data", {})
  if data is None:
      data = {}
  if not isinstance(data, dict):
      raise ToolError("event.data must be an object")
  ```

- **Regression test:** Add a parametrized case (`data=[]`, `data=0`, `data=""`, `data=False`) to `tests/pi/test_v8_cherrypick_fixes.py` so the params and data variants of the same bug-class live together. For `notify_event` assert the dispatch is rejected / logged; for `event` assert a wire-level error response.

### F2: Bridge envelope `event` is not authoritative — mismatched `data.type` can dispatch the wrong Python hook

- **Severity:** HIGH

- **Reviewer agreement:** 2 reviewers (codex-spec-wire, codex-gen-1).

- **Location:** `src/libharness/pi/server.py:198-203`, `src/libharness/pi/shim.py:271-289`.

- **Observed:** `_dispatch_bridge_event` reads `event_name = str(request.get("event") or "")` then does `event = dict(data); event.setdefault("type", event_name)`. The `setdefault` means the inner `data.type` wins if present; the outer envelope `event` field only fills in when missing. A shim (or attacker) that sends `{"event":"tool_call","data":{"type":"session_compact",...}}` dispatches to the `session_compact` hook with `tool_call` semantics.

- **Why wrong:** Silent dispatch of malformed input — the user's `on_<event>` handler receives an event whose envelope name (the gate the bridge subscribed to) does not match the type the handler sees. Type-confusion within the named-hook surface.

- **Suggested fix:** Assert agreement when both are present:

  ```python
  inner_type = event.get("type")
  if inner_type is not None and inner_type != event_name:
      raise ToolError(f"bridge envelope event={event_name!r} does not match data.type={inner_type!r}")
  event["type"] = event_name  # envelope wins
  ```

- **Regression test:** Send a notify_event with mismatched envelope/inner type; assert the dispatch is rejected and no hook fires.

### F3: UI handler exception kills the RPC stdout reader; harness goes silently deaf

- **Severity:** HIGH

- **Reviewer agreement:** 2 reviewers (opus-gen-1 F2, codex-spec-rpc).

- **Location:** `src/libharness/pi/rpc.py:417-438` (`_handle_extension_ui_request`).

- **Observed:** The handler is invoked with no `try/except` (line 428-429). Any exception propagates up through `_handle_message` (line 405) to `_read_stdout_loop` (line 382) and kills the reader task. Opus-gen-1 reproduced: exception escaped; no wire frame written.

- **Why wrong:** UI handlers are user-supplied (`set_extension_ui_handler`). A non-trivial handler that hits `NameError`, `AttributeError`, or a third-party exception tears down the entire RPC client: every pending request times out, every future event from pi is lost, the framework's "default response on no handler" path is bypassed, pi's UI-side hangs until its own timeout fires. Silent correctness / silent dispatch failure.

- **Suggested fix:** Wrap the handler invocation in try/except + log + fall through to the default response. Don't suppress `CancelledError`:

  ```python
  if handler is not None:
      try:
          maybe = handler(request)
          response = await maybe if inspect.isawaitable(maybe) else maybe
      except asyncio.CancelledError:
          raise
      except Exception:
          _LOG.exception("UI handler failed for method %s", method)
          response = None
  if response is None:
      response = self._default_ui_response(method)
  ```

- **Regression test:** Install a UI handler that raises; call `_handle_extension_ui_request`; assert no exception escapes, the wire frame written is the default response with the correct `id`, and a subsequent call still works (client isn't poisoned).

### F4: RPC event subscriber exception kills the reader (same shape as F3)

- **Severity:** HIGH
- **Reviewer agreement:** 2 reviewers (opus-gen-1 F3, codex-spec-rpc).
- **Location:** `src/libharness/pi/rpc.py:410-415` (`_dispatch_event`).
- **Observed:** The loop `for handler in list(self._event_handlers): result = handler(event); ...` has no try/except. An exception from any subscriber kills the reader and silently deafens the harness. Opus-gen-1 reproduced.
- **Why wrong:** This is load-bearing for `Agent._async_on_event` (installed via `client.on_event(...)`). In strict mode, `_async_on_event` raises `UnhandledEventError` for any unknown event type — pi can and will emit events the Agent's surface doesn't enumerate. One unknown event → one-shot reader death → silent harness. Strict-mode users are the exact users who care about this being reported instead of silenced.
- **Suggested fix:** Mirror F3 — try/except inside the loop, log, continue. Re-raise `CancelledError`. Sibling fix should go in the same commit as F3 for symmetry.
- **Regression test:** Subscribe two handlers, one raises, one records. Fire an event; assert the recorder ran, no exception escaped, the second `_dispatch_event` call still calls both. Add a strict-mode end-to-end test that fires an unknown event and asserts the harness stays alive.

### F5: Bridge `notify_event` failures swallow malformed-JSON / UTF-8 / timeout into bare EOF

- **Severity:** HIGH (silent dispatch of malformed input — same severity class as F1/F2 above)
- **Reviewer agreement:** 2 reviewers (codex-spec-wire MODERATE, codex-gen-2 MINOR; promoted to HIGH because this is silent correctness loss combined with the F1/F2 cluster).
- **Location:** `src/libharness/pi/server.py:138-162` (`_serve_client`).
- **Observed:** When the initial `readuntil`/`feed` raises (timeout, decode error, invalid JSON), `request` is still `None`. The outer `except Exception` then guards `if request is not None and request.get("type") != "notify_event"` — so the failure produces no wire frame, just an EOF on the client side. The TS shim sees a bare disconnect; the operator gets nothing in the logs unless the server-side `_LOG.exception` path is reached (it is in `_dispatch_bridge_event` but not at the outer `_serve_client` level for the malformed-frame case).
- **Why wrong:** Same class as the F1/F2/F3 silent-failure family. A misbehaving shim or a frame that splits across a TCP edge silently disappears. For `notify_event` specifically the silence is intentional (no response is expected) — but the error case isn't logged either; whoever reads server logs has no signal.
- **Suggested fix:** Add an `_LOG.warning("malformed bridge request", exc_info=True)` (or `exception(...)`) in the outer `except Exception` for the `request is None` case. Keep "no wire frame for notify_event" — but make the log present.
- **Regression test:** Open a bridge connection; send invalid JSON; assert (a) no wire frame is written, (b) a WARNING was logged with the cause.

### F6: 64 KiB `readuntil` ceiling on the bridge silently truncates large frames

- **Severity:** HIGH

- **Reviewer agreement:** 1 reviewer (codex-gen-1). Listed Tier-1 because it's a silent data-loss failure with a one-line fix.

- **Location:** `src/libharness/pi/server.py:139-141`.

- **Observed:** `await reader.readuntil(b"\n")` uses `StreamReader`'s default buffer cap of 64 KiB. The bridge's `StrictJsonlDecoder` accepts 8 MiB, but the underlying reader never lets a frame larger than 64 KiB reach the decoder — `readuntil` raises `LimitOverrunError` on the wire. The exception path in `_serve_client` swallows the failure to bare EOF for notify_event (see F5).

- **Why wrong:** Decision events legitimately carry large payloads (full message arrays in `session_compact`, large tool argument blobs in `tool_call_decision`). A real-world bridge frame >64 KiB silently disappears.

- **Suggested fix:** Pass an explicit `limit=` to the stream creation, or to `readuntil`:

  ```python
  reader, writer = ...  # already created by asyncio.start_server
  chunk = await asyncio.wait_for(
      reader.readuntil(b"\n"), timeout=...
  )
  ```

  Either bump the `StreamReader` limit at server-start time (`asyncio.start_server(..., limit=8 * 1024 * 1024)`) to match the decoder's 8 MiB budget, or set it on the stream. Match the decoder's `max_buffer_bytes` (`8 * 1024 * 1024`).

- **Regression test:** Send a single bridge frame of 1 MiB; assert it is dispatched and a response (or notify ack) is observed. Without the fix, the test sees EOF.

### F7: `AsyncioLoopThread.start()` and `PiAgentHarness.start_owner_thread()` drop their locks before waiting — concurrent callers spawn duplicate threads

- **Severity:** HIGH
- **Reviewer agreement:** 1 reviewer (codex-spec-thread, two related findings). Listed Tier-1 because it's a silent corruption (two threads claim ownership; futures get routed to a dead one) with a one-line fix.
- **Location:** `src/libharness/pi/runtime.py:56-71` (`AsyncioLoopThread.start`); `src/libharness/pi/agent.py:146-159` (`start_owner_thread`).
- **Observed:** In `AsyncioLoopThread.start`, the `_start_lock` is held only across the thread construction and `_thread.start()`; `_ready.wait()` is *outside* the `with`. Two threads racing into `start()` can both pass the `if self.is_running` check (the loop hasn't set itself running yet from the first thread's perspective), each constructs and starts a `Thread`, and the second one stomps `self._thread`. Only the most recent thread is joined by `stop()`. Similarly in `start_owner_thread`, the `if self._owner_thread is not None` check is unguarded and the subsequent assignment is too. The first owner thread will trip `_owner_ready.set_result(None)` and a second will hit `InvalidStateError`.
- **Why wrong:** `_call`/`submit` route work to `_owner_thread`'s queue; if a duplicate ran first, the queue is owned by the wrong thread. Silent corruption — the visible symptom is `InvalidStateError` or futures that never complete.
- **Suggested fix:** Move `_ready.wait()` (and the owner equivalent) *inside* the `with self._start_lock:` block. For `start_owner_thread`, the entire "check + assign + start + wait" sequence belongs in a lock. Trivial.
- **Regression test:** N concurrent callers of `start_owner_thread()` (or `AsyncioLoopThread.start()`); assert exactly one thread was created and all callers observe the same `_owner_thread.ident`.

## Tier 2 — defer to post-port ergonomic pass or Phase 6 test work

### F8: Shared `hook_executor` with `max_workers=1` across the default runtime serializes HITL across harnesses

- **Severity:** HIGH (reviewers); my read: HIGH but design decision
- **Reviewer agreement:** 3 reviewers (opus-gen-1 F6, opus-gen-2 F1, codex-spec-thread).
- **Location:** `src/libharness/pi/runtime.py:142-145`; consumers at `src/libharness/pi/agent_class.py:124, 158`.
- **Observed:** `HarnessRuntime.__init__` creates one `ThreadPoolExecutor(max_workers=1, ...)` shared by sync notification and sync decision hooks. `get_default_runtime()` returns a process-wide singleton; all `Agent` instances share it by default. A long-running sync `decide_session_compact` (legitimate HITL use, per the carried-forward author resolution that `_decision_timeout_ms = None`) blocks every other sync hook on every other agent sharing the runtime.
- **Why deferred:** Contradicts no author resolution outright but does interact with the HITL default. The fix space (per-Agent pool? bump `max_workers`? doc the contention?) is a design decision, not a one-line repair. Reviewer agreement is high but no reviewer found a one-line fix.
- **Recommended Tier-2 action:** Add a docstring note on `HarnessRuntime.hook_executor` and on `Agent`'s sync-hook section calling out the cross-harness serialization and recommending `async_decide_<event>` for HITL. Revisit the design (per-Agent vs shared) in the ergonomic pass.

### F9: `PiAgentHarness._call` / `submit` race with `close()` — commands queued after `_STOP` never complete

- **Severity:** HIGH (reviewer) / HIGH (my read)
- **Reviewer agreement:** 1 reviewer (codex-spec-thread).
- **Location:** `src/libharness/pi/agent.py:165-179, 240-267, 275-282`.
- **Observed:** `close()` sets `_closed = True` then puts `_STOP` on the queue. Between the check `if self._closed: raise` in `submit()` and the `self._commands.put(command)` two lines later, another thread can call `close()`. The `_STOP` reaches the queue first; the late command sits behind it forever. The caller's `future.result()` never returns.
- **Why deferred:** HIGH but a non-trivial design call (do we acquire a lock around `_closed`+`_commands.put`? Do we add a deadline to `_call`?). No reviewer found a one-line fix; only one reviewer flagged it. Doesn't affect the test surface today because the 13 tests don't exercise threaded `close()` under concurrent `_call`.
- **Recommended Tier-2 action:** Phase 6 test port should add a regression test that calls `close()` while a `_call` is in flight; the fix (a `_close_lock` guarding the `_closed`/`_commands.put` pair, with `_call` short-circuiting under the same lock) lands in the ergonomic pass.

### F10: Keyword-only `ctx`/`context` decision hooks fail at runtime — heuristic detects them but dispatcher invokes positionally

- **Severity:** HIGH (reviewer) / MODERATE (my read — covered by author resolution Item D)
- **Reviewer agreement:** 1 reviewer (codex-spec-hooks).
- **Location:** `src/libharness/pi/agent_class.py:210-211, 215-231`.
- **Observed:** `_handler_wants_context` returns `True` when the signature has a keyword-only `ctx` or `context` param. Then `_call_decision_handler(handler, event, ctx)` calls `handler(event, ctx)` *positionally*. Python raises `TypeError: handler() takes 2 positional arguments but 3 were given` for `def decide_X(self, event, *, ctx)`.
- **Why deferred:** Contradicts author resolution Item D — defer until the ergonomic pass. The bug surface is narrow (users who specifically write keyword-only `ctx` and don't use `*args`). The author already chose to defer the `_handler_wants_context` heuristic cleanup.
- **Recommended Tier-2 action:** Fold into the Item-D ergonomic pass. Either reject keyword-only `ctx` at subclass time (validator) or call `handler(event, ctx=ctx)` when the heuristic detects a keyword-only ctx.

### F11: `_decision_timeouts_ms` is a shared mutable class-attribute — cross-subclass mutation leaks

- **Severity:** MODERATE (reviewers) / MODERATE (my read)
- **Reviewer agreement:** 2 reviewers (opus-gen-1 F4, opus-gen-2 F7).
- **Location:** `src/libharness/pi/hook_surface.py:82`.
- **Observed:** Default `_decision_timeouts_ms: ClassVar[dict[str, int]] = {}` is shared by every subclass that doesn't reassign. A user who mutates `MyAgent._decision_timeouts_ms["tool_call"] = 5000` mutates the *base* `AgentHookSurface` dict, affecting every other subclass. Opus-gen-1 reproduced live.
- **Why deferred:** Classic Python footgun; library reads only and copies via `dict(...)` before use, so library behavior is safe. User-visible only when users mutate (not reassign). Two-reviewer agreement, but no silent correctness or security implication on the library side.
- **Recommended Tier-2 action:** Change the default to `MappingProxyType({})` (raises on mutation) or document "reassign, don't mutate" in the class docstring. Land in the ergonomic pass.

### F12: `Agent.__init__` silently overwrites three user-provided kwargs

- **Severity:** MODERATE / MINOR
- **Reviewer agreement:** 2 reviewers (opus-gen-1 F9, opus-gen-2 F3).
- **Location:** `src/libharness/pi/agent_class.py:56-61`.
- **Observed:** `kwargs["bridge_event_handler"] = self._async_on_bridge_event` (and `initial_open_gates`, `decision_timeouts_ms`) unconditionally overrides whatever the user passed. No warning.
- **Why deferred:** Documentation issue, not a correctness issue. The override is intentional per Agent's design (the Agent owns these).
- **Recommended Tier-2 action:** Raise `TypeError("Agent owns bridge_event_handler / initial_open_gates / decision_timeouts_ms — set via class attributes or subclass methods")` when one is in `kwargs`. One-time docstring update on `Agent`.

### F13: `watch_disconnect` in the bridge server sets `cancelled` in its `finally` on normal completion

- **Severity:** MODERATE / MODERATE
- **Reviewer agreement:** 1 reviewer (opus-gen-2 F5).
- **Location:** `src/libharness/pi/server.py:206-216`.
- **Observed:** When the parent task finishes cleanly, `watcher.cancel()` runs; the watcher's `finally` still executes `cancelled.set()`. `HookContext.cancelled` becomes True after a normal completion.
- **Why deferred:** No current code path consults `ctx.cancelled` after the hook returns, so this is latent. But the semantic is "did the connection drop" and the implementation is "did we ever stop watching" — a future ergonomic-pass fix should disambiguate.
- **Recommended Tier-2 action:** Distinguish "watcher saw EOF" from "watcher cancelled by completion." Land with F8/F10/F11/F12 in the ergonomic pass.

### F14: Reader-task death does not terminate the Pi subprocess; later RPCs just time out

- **Severity:** HIGH (reviewer) / MODERATE (my read — symptom of F3/F4 fixed elsewhere)
- **Reviewer agreement:** 1 reviewer (codex-spec-rpc).
- **Location:** `src/libharness/pi/rpc.py:372-385` (`_read_stdout_loop`); reaches via `_handle_message` / `_handle_extension_ui_request`.
- **Observed:** Exceptions in `_read_stdout_loop` or `_handle_extension_ui_request` kill the stdout reader but leave the pi subprocess alive. Later RPCs hit the request timeout.
- **Why deferred:** Once F3 and F4 are fixed, the only remaining path to reader death is something inside `_read_stdout_loop` itself (decoder error, asyncio bug). At that point, the right answer is a separate ergonomic-pass decision: either tear down the subprocess on reader-task crash (failure cascade), or surface a `PiRpcProcessError` on the next `send()` rather than silently timing out. Both are design changes.
- **Recommended Tier-2 action:** After F3/F4 land, decide between "kill pi if reader dies" vs "fail pending and raise on next send."

### F15: `close()` can hang if reader is awaiting a handler that swallows `CancelledError`

- **Severity:** HIGH (reviewer) / MODERATE (my read)
- **Reviewer agreement:** 1 reviewer (codex-spec-rpc).
- **Location:** `src/libharness/pi/rpc.py:207-238`.
- **Observed:** `close()` calls `task.cancel()` then `await asyncio.gather(task, return_exceptions=True)`. If the reader is currently inside an awaited UI handler or event subscriber that swallows `CancelledError`, the await hangs.
- **Why deferred:** Same root cause as F3/F4 (well-behaved handlers don't swallow cancellation; the framework should re-raise). Once F3/F4 explicitly `raise` on `CancelledError`, this hazard narrows. Add a bounded `wait_for` on the gather in the ergonomic pass.
- **Recommended Tier-2 action:** Bound the gather in `close()` with `asyncio.wait_for(..., timeout=2.0)`; on timeout, log and move on.

### F16: `ProcessLookupError` during SIGTERM aborts `close()` before clearing `_pending` / `self.process`

- **Severity:** MODERATE / MODERATE
- **Reviewer agreement:** 1 reviewer (codex-spec-rpc).
- **Location:** `src/libharness/pi/rpc.py:218-238`.
- **Observed:** If the subprocess exited between `proc.wait()` timeout and the `proc.send_signal(SIGTERM)` retry, `send_signal` raises `ProcessLookupError`. The `except TimeoutError` branch doesn't catch it; close() exits early before reaching the `_fail_pending` / `self.process = None` cleanup.
- **Why deferred:** Narrow race window; benign in practice (pi already exited, pending requests were already going to fail). Easy fix (`contextlib.suppress(ProcessLookupError)`) but no reviewer agreement.
- **Recommended Tier-2 action:** Wrap the SIGTERM/wait/kill block in `contextlib.suppress(ProcessLookupError)` and ensure the `_fail_pending` / cleanup at the end always runs.

### F17: Bridge server, RPC client, and the 13-test suite do not exercise `Agent` at all

- **Severity:** MODERATE (test-gap)
- **Reviewer agreement:** 2 reviewers (opus-gen-2 F10, codex-gen-2).
- **Location:** `tests/pi/`.
- **Observed:** None of the 13 tests import `Agent`, `AgentEvent`, `HookContext`, `AgentHookSurface`, or `UnhandledEventError`. The dispatcher, the gate model, the 38 new ClassVars, the manifest's `initialOpenGates` / `decisionTimeoutsMs` fields, and the bridge `event` / `notify_event` request types are not touched.
- **Why deferred:** Phase 6 is the test port. Author has already scoped it.
- **Recommended Tier-2 action:** Phase 6 owns this. Add at minimum a smoke test that subclasses `Agent`, fires a fake decision event through `_async_on_bridge_event` directly, asserts the decision hook is called with expected args.

### F18: Other Tier-2 findings (grouped MINOR / MODERATE)

Brief catalog of single-reviewer MODERATE / MINOR findings that map to deferred items or are not load-bearing:

| ID  | Loc                                            | Finding                                                                          |
| --- | ---------------------------------------------- | -------------------------------------------------------------------------------- |
| F19 | `agent_class.py:196-202`                       | `_decision_timeouts_for_manifest` advertises timeouts for closed gates           |
| F20 | `server.py:273`                                | `metadata = dict(request.get("context") or {})` — same falsy-coercion as F1      |
| F21 | `hook_surface.py:205, 240`                     | `AgentHookSurface` subclasses can bypass validation; non-callable hook vals      |
| F22 | `agent_class.py:121-127`                       | Sync notification hooks returning awaitables: dispatcher drops them silently     |
| F23 | `hook_surface.py:265-277`                      | Timeout validator accepts non-int values like `True` / `0.5`                     |
| F24 | `rpc.py:152-154, 184-187`                      | Failed `start()` leaves `self.process` set, blocking retry                       |
| F25 | `agent.py:89, 121`                             | Constructor-passed `decision_timeouts_ms` skips validation                       |
| F26 | `agent_class.py:144-146` + `server.py:154-162` | Decision-hook exceptions logged twice (analysis Item A; deferred)                |
| F27 | `hook_surface.py:32-77` + `shim.py:79-99`      | `_DECISION_EVENT_NAMES` subclass override desyncs from shim's array (Item B)     |
| F28 | `agent_class.py:234-235`                       | `cast(object, Agent)` workaround needs an expanded comment                       |
| F29 | `rpc.py:57, 177`                               | `PiLaunchConfig.startup_timeout` misnamed (probe sleeps ≤0.2s)                   |
| F30 | `tests/pi/test_v8_cherrypick_fixes.py:114-141` | Test asserts state but not order of cleanup                                      |
| F31 | `hook_surface.py:321`                          | `_assert_declarations_match_event_sets` only runs on base; subclass drift latent |
| F32 | `agent.py:485-491`                             | `_check_owner` error message doesn't include harness_id                          |
| F33 | `rpc.py:178-181` (next_event/wait)             | `next_event()`/`wait_for_event()` waiters never unblocked on `close()`           |
| F34 | `rpc.py:240-268`                               | Caller-supplied duplicate request IDs can overwrite `_pending` entries           |
| F35 | `tools.py` (ToolContext registration)          | Tool params named `context`/`ctx` get silently hijacked for `ToolContext`        |
| F36 | `shim.py:159-160, 245-246`                     | Same key-precedence footgun (Phase 5.5 dict-merge) in `bridgeCall/bridgeNotify`  |

**Detail:**

- *F19, F20:* Same family as F1 / F2. F20 in particular is `dict(request.get("context") or {})` — same `or {}` pattern; can defer because metadata is informational, but worth folding into the F1 fix commit if it's cheap.
- *F21:* Subclasses with `class M(AgentHookSurface)` then `class N(M, Agent)` can stash a non-callable hook attribute that survives until dispatch. Edge case; not a port bug.
- *F22:* Sync handler returning a coroutine: the `run_in_executor` yields the coroutine object as the result and discards it. Same heuristic-fragility class as F10.
- *F23:* Validator does `isinstance(value, int)`; `True == 1` and `0.5` would slip if the type were `Number`. Mostly cosmetic.
- *F24:* After `_cleanup_after_startup_failure`, `self.process` is not nulled; a retry hits "already started." Easy fix but not load-bearing.
- *F26:* Author resolution Item A — deferred to ergonomic pass.
- *F27:* Author resolution Item B — deferred. The synth doc agrees.
- *F29:* The 0.2-second probe is the most plausible flake source in slow CI; opus-gen-2 ran 5x clean locally. Worth a Phase 6 stability check.
- *F30:* Test asserts the slot is null at function exit, not that cancel/gather ran in the right order. Stronger test would intercept the task refs mid-startup.
- *F33, F34:* Two related rpc-client lifecycle hazards. F33 (`next_event` waiter never woken on close) is a known asyncio queue pattern hazard; F34 (caller-supplied ID collision) is mitigated by the framework generating IDs but exposed because `send()` accepts an override.
- *F35:* Tool decorator inspects parameter names; a tool whose own `params` schema includes `context` collides. Phase 6 doc should warn.
- *F36:* The Phase 5.5 dict-merge fix in `rpc.py:411` has a sibling in `shim.py`'s TS `bridgeCall/bridgeNotify`. Same one-line spread reorder.

## De-duplication notes

| Tier-1 ID | Reviewers (where this finding appeared)                                           |
| --------- | --------------------------------------------------------------------------------- |
| F1        | opus-gen-1 F1 (HIGH), codex-spec-wire (HIGH)                                      |
| F2        | codex-spec-wire (HIGH), codex-gen-1 (HIGH)                                        |
| F3        | opus-gen-1 F2 (HIGH), codex-spec-rpc (HIGH)                                       |
| F4        | opus-gen-1 F3 (HIGH), codex-spec-rpc (HIGH, implied via "exception kills reader") |
| F5        | codex-spec-wire (MODERATE), codex-gen-2 (MINOR)                                   |
| F6        | codex-gen-1 (HIGH)                                                                |
| F7        | codex-spec-thread #1+#2 (HIGH, two related findings)                              |

| Tier-2 ID | Reviewers                                          |
| --------- | -------------------------------------------------- |
| F8        | opus-gen-1 F6, opus-gen-2 F1, codex-spec-thread #4 |
| F9        | codex-spec-thread #3                               |
| F10       | codex-spec-hooks #1                                |
| F11       | opus-gen-1 F4, opus-gen-2 F7                       |
| F12       | opus-gen-1 F9, opus-gen-2 F3                       |
| F13       | opus-gen-2 F5                                      |
| F14       | codex-spec-rpc                                     |
| F15       | codex-spec-rpc                                     |
| F16       | codex-spec-rpc                                     |
| F17       | opus-gen-2 F10, codex-gen-2                        |
| F19       | opus-gen-1 F5                                      |
| F20       | opus-gen-1 F7                                      |
| F21       | codex-spec-hooks #2 + opus-gen-2 F9                |
| F22       | codex-spec-hooks #3                                |
| F23       | codex-spec-hooks #4                                |
| F24       | opus-gen-1 F10                                     |
| F25       | opus-gen-1 F11                                     |
| F26       | opus-gen-1 F12                                     |
| F27       | opus-gen-1 F13                                     |
| F28       | opus-gen-1 F14                                     |
| F29       | opus-gen-2 F4                                      |
| F30       | opus-gen-2 F6                                      |
| F31       | opus-gen-2 F8                                      |
| F32       | opus-gen-2 F12                                     |
| F33       | codex-spec-rpc                                     |
| F34       | codex-spec-rpc                                     |
| F35       | codex-gen-1                                        |
| F36       | codex-spec-wire                                    |

## Process notes

- All 8 reviewers ran in parallel against the same commit (`de0fe82`).
- Most reviewers reported `make all` green on both `local.venv` (3.11) and `local-ft.venv` (3.14t); flake-checks (3-5 iterations) clean across the board.
- Codex reviewer outputs in `/tmp/v8-port-review-codex-*.md` are summary-only — each codex review file contains its own bottom-line, not a per-finding breakdown like the Opus files. Where line numbers came from a summary alone, I verified them against the source before listing in this synthesis.
- Author resolutions honored: `AgentHookSurface` stays public, `PiPythonHarness` stays for v1, three-file split adopted, `_decision_timeout_ms = None` default preserved (HITL case), Items A/B/D/F deferred, Items C/E land in Phase 6. Where a reviewer recommended a change that contradicts these (notably F10 / Item D and F26 / Item A), I marked the finding Tier-2 with a note pointing at the resolution.
- The codex generalist reviews flagged two findings that no other reviewer raised (F6 `readuntil` 64 KiB ceiling, F35 tool-param-name collision). Both are concrete; F6 promoted to Tier-1 because it's silent data loss with a one-line fix.
- The dominant theme across all reviewers: the v8 port carries forward several silent-failure patterns (falsy-coercion, exception-eats-reader, lock-drop races) that Phase 5.5 only fixed at one site each. The Tier-1 list closes those patterns out at every sibling site we know about.
- No reviewer found a bug specific to the three-file split, the `AgentHookSurface` mixin extraction, or the 38-decl gap closure (Phases 2-5). The structural changes appear sound.

Recommendation: apply Tier-1 fixes F1-F7 in two commits (F1+F2+F5+F6 to `server.py` as the bridge-input-validation commit; F3+F4+F7 across `rpc.py` + `runtime.py` + `agent.py` as the user-callback-and-startup-race commit), then proceed to Phase 6.
