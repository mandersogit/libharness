---
status: Resolved
created: '2026-05-15'
---

# Concurrency model: asyncio vs threads (with freethreading consideration)

## Resolution (2026-05-15)

**Decision:** Primarily **D** — threads on freethreaded CPython 3.14t, optimizing for freethreading parallelism opportunities. Backwards-compatible with **C** — the same code runs on standard CPython 3.11+ under the GIL, verified by `make test-311`. Sync `def` for tool functions and `on_*` hooks; `async def` is rejected at decoration time (not-pi-2 pattern).

This matches the author's `Agent`-with-`on_*`-hooks design intent and aligns libharness with the three-iteration family precedent (hildy, simple-harness, not-pi-2). The dual-venv scaffolding (`local.venv` 3.11 + `local-ft.venv` 3.14t, `make all` runs both) is the verification surface. The asyncio → threads rewrite of `src/libharness/pi/{rpc_client,server,tools}.py` and the `PiPythonHarness` lifecycle is the next implementation milestone; see SESSION-STATE.md § Pending tasks for current shape and gating.

Everything below this section is the original co-design analysis, retained for rationale and historical context. Decision points #1–#4 in § Decision points are now answered as recorded above; #5 (Agent class design + concrete `on_*` hook set) is a follow-up co-design.

## What's being decided

How libharness organizes concurrency internally and what programming model it presents to tool authors and `Agent` subclassers. Three sub-questions, which are mostly but not entirely independent:

1. **Programming model:** asyncio (single-threaded cooperative) vs threads (preemptive).
1. **Authoring style for tools and event hooks:** sync `def` vs `async def`.
1. **Python version baseline and freethreading:** standard CPython (3.11+) vs freethreaded CPython 3.14t.

The current code (v5 verbatim port) is asyncio top-to-bottom and runs on standard Python 3.11. The author has stated a preference for an `Agent` class with `on_*` hook methods for event handling. The author also raised Python 3.14 freethreading as an option worth weighing.

## What's actually concurrent in libharness

Concrete inventory of independent activities that the harness has to manage:

| Activity                      | Cardinality                | Typical duration | Workload               |
| ----------------------------- | -------------------------- | ---------------- | ---------------------- |
| Pi stdout reader              | 1 long-running             | indefinite       | I/O blocked            |
| Pi stderr reader              | 1 long-running             | indefinite       | I/O blocked            |
| Pi stdin writer               | bursts                     | μs per call      | I/O                    |
| Bridge listener (accept loop) | 1 long-running             | indefinite       | I/O blocked            |
| Bridge connection handlers    | 0..N (pi-driven)           | tool duration    | I/O + user code        |
| User tool functions           | 0..N (matches connections) | wildly variable  | user-determined        |
| User `on_*` event handlers    | bursts per pi event        | typically μs–ms  | user-determined        |
| Harness API caller ("main")   | 1 long-running             | session duration | calls into the harness |

Two facts matter most for the model choice:

1. The fixed plumbing is a *small constant number* of I/O streams (4 long-running readers/listeners). Not thousands of concurrent sockets — asyncio's structural strength does not apply.
1. The variable workload — tool functions and `on_*` handlers — is **user code**. We don't get to constrain its shape. CPU-bound tools and blocking I/O calls are both realistic.

## Family precedent

| Project              | Programming model      | Tool authoring                    |
| -------------------- | ---------------------- | --------------------------------- |
| hildy                | threads                | sync                              |
| simple-harness       | sync (single-threaded) | sync; rejects async at decoration |
| not-pi-2/src/harness | sync (single-threaded) | sync; rejects async at decoration |
| libharness (today)   | asyncio                | sync OR async                     |

**Detail:**

- *hildy:* Reimplements pi from scratch in Python.
- *simple-harness:* `Iterator[StreamChunk]` for streams (sync, not async iterator).
- *not-pi-2/src/harness:* Parallelism is a separate concern — subclass `ToolDispatcher` with a `ThreadPoolExecutor`.
- *libharness (today):* Inherited from v5 port; was never a considered choice.

Three independent iterations in this lineage converge on sync. Libharness is the outlier and only by inheritance, not deliberation.

## Why the `on_*` hooks design effectively forces the answer

The author's stated intent is "an `Agent` class with a variety of `on_*` methods that are my hooks for responding to the events." That's a Template Method pattern — subclassers override hook methods to react.

Under asyncio, every overrideable hook must be `async def`. A subclasser who wants `on_tool_call` to log a line writes:

```python
class MyAgent(Agent):
    async def on_tool_call(self, event):
        logger.info("tool call: %s", event.name)
```

That `async` is wrong-shaped: there's no `await` in the body. The user has to know they need it. Mixing — some hooks sync, some async — is worse: footgun + mypy noise + asyncio detecting "you returned a coroutine that was never awaited."

The Pythonic stdlib precedent (`logging.Handler.emit`, `http.server.BaseHTTPRequestHandler.do_GET`, `unittest.TestCase.setUp`) is sync override hooks. The author's design intent matches that precedent.

**This alone, independent of any other consideration, is enough to lean strongly toward sync.**

## On pi being async

Pi is async because it runs on Node, which has one event loop and asynchronous I/O is the only flavor available. We are not pi. We talk to pi via JSONL on its stdin/stdout. Our internal model is uncoupled from pi's. There's no integration benefit to mirroring pi's choice.

## Options

### A. Stay asyncio, patch the tool-ergonomics leaks

Current state plus two surgical fixes: `ctx.update` callable from sync contexts; sync tool handlers run on the loop's default executor. Public API stays async.

- **Pro:** smallest change. Tests stay as they are.
- **Con:** every `on_*` hook subclasser writes `async def`. Asynchronous coloring leaks across the public surface even though it's not needed.
- **Con:** family-pattern outlier.

### B. Sync facade over async core

Keep all current asyncio internals. Add a `SyncPiPythonHarness` that runs the asyncio loop in a background thread and exposes a sync `with` + sync method calls (v1 used this pattern; it worked).

- **Pro:** preserves the current code; adds an opt-in sync surface.
- **Con:** two paradigms to maintain. Subtle seam bugs around loop lifecycle and exception propagation.
- **Con:** doesn't fix the `on_*` hooks problem unless we also rewrite the Agent class as sync (at which point the async core isn't earning its keep).

### C. Threads, standard CPython 3.11+

Rewrite to a thread-based model:

- `subprocess.Popen` + reader threads for pi stdout/stderr.

- `socket` + thread-per-connection for the bridge (or `socketserver.ThreadingTCPServer`).

- Request/response correlation via `dict[id, queue.Queue]` protected by a lock.

- Cancellation via `threading.Event`.

- Tool functions are sync; reject `async def` at decoration time (per not-pi-2's pattern).

- `Agent.on_*` hooks are sync overridable methods. Default body is `pass`.

- `pytest`, no `pytest-asyncio`.

- **Pro:** aligns with three prior iterations.

- **Pro:** no async coloring anywhere. Subclassing `Agent` is idiomatic Python.

- **Pro:** a user's `requests.get()` in a tool doesn't hang the harness.

- **Con:** ~1-day rewrite. v5's cancellation/cleanup edge cases need re-debugging in the thread idiom.

- **Con:** GIL serializes parallel CPU-bound Python tools (mitigation: option D).

### D. Threads, freethreaded CPython 3.14t (orthogonal-ish to C)

Same code as C, but tested and deployed against Python 3.14t (the freethreaded build). All `threading` primitives work identically; freethreading just removes the GIL, so true parallelism is available for CPU-bound Python.

- **Pro:** parallel CPU-bound tool execution actually parallelizes.
- **Pro:** future-proofs against the broader Python ecosystem shift.
- **Con:** ecosystem compatibility is still maturing. C extensions that aren't freethreading-aware silently re-enable the GIL when imported, partially defeating the purpose.
- **Con:** Python 3.14 is recent; user audience smaller in 2026.
- **Con:** orthogonal to the programming-model question — D is "C plus a recommended deployment target."

These can be combined: **C is the code change; D is the recommended runtime story.** Code written for C runs unchanged on 3.14t. Nothing in C precludes freethreading.

## Trade-off matrix

| Concern                                   | A: asyncio       | B: sync facade           | C: threads (3.11+)         | D: threads (3.14t) |
| ----------------------------------------- | ---------------- | ------------------------ | -------------------------- | ------------------ |
| `Agent.on_*` hooks read as Pythonic       | no               | partial                  | **yes**                    | **yes**            |
| Tool author writes `def`, not `async def` | partial          | yes                      | **yes**                    | **yes**            |
| Match family idiom                        | no               | no                       | **yes**                    | **yes**            |
| Long-running sync code in tools is safe   | no (blocks loop) | depends on which surface | **yes**                    | **yes**            |
| Parallel CPU-bound tools parallelize      | no               | no                       | no (GIL)                   | **yes**            |
| Rewrite cost from current state           | small (~30 LOC)  | medium (~200 LOC)        | medium (~1 day)            | same as C          |
| Python version requirement                | 3.11+            | 3.11+                    | 3.11+                      | **3.14t+**         |
| Test rewrite cost                         | none             | small                    | full (drop pytest-asyncio) | same as C          |
| Ecosystem maturity                        | full             | full                     | full                       | partial            |

**Detail:**

- *A — Tool author writes `def`:* "partial" because sync tools work today and async tools work too, but `ctx.update` is only awaitable, which leaks async into otherwise-sync code.
- *B — Rewrite cost:* the ~200 LOC is for the sync-facade wrapper (loop-in-background-thread + `run_coroutine_threadsafe` plumbing) on top of the existing async core.

## Recommendation

**C, with D as the recommended-but-not-required deployment.** High confidence.

Reasoning:

1. The `on_*` hooks design intent is the decisive factor by itself. Pythonic subclass hooks are sync; making them async would be wrong-shaped for the intended use.
1. Family alignment matters more than I initially weighted. Three independent iterations chose sync; libharness diverged only by inheriting v5's port. Aligning means tool code is portable across the four projects, and contributors don't context-switch.
1. asyncio's structural strength (massive concurrent I/O) is not our workload. We have four long-running readers and a small bursty connection-handler set. Threads are at least as good a fit, with simpler debugability.
1. Freethreading should be supported as a runtime option but not required. The code that runs under C runs under D unchanged; we just verify it. This buys CPU-bound parallelism for users who opt in without raising the floor for everyone else.
1. The cost is a focused day of rewriting plus regression debugging for cancellation/cleanup. Acceptable now (no users, no apps); much costlier later.

The version of the recommendation I am *not* making: pure async-internals + sync-facade (B). It's the worst of both: two paradigms to maintain, the bug surface of the seam, and it doesn't make the `on_*` hooks problem any easier.

## What this implies for tool authoring and event hooks

If C is adopted:

```python
class MyAgent(libharness.pi.Agent):
    # Sync overrides. Default bodies are `pass`.
    def on_tool_call(self, event: ToolCallEvent) -> None:
        logger.info("tool call: %s", event.name)

    def on_agent_end(self, event: AgentEndEvent) -> None:
        self.save_transcript(event.messages)

@my_agent.tool(description="Add two integers")
def add(a: int, b: int) -> str:
    return str(a + b)

with MyAgent(...) as agent:
    agent.prompt("Use add to compute 41 + 1")
    # blocks until agent_end; on_* hooks fire on the harness thread
```

No `async`. No `await`. No `pytest-asyncio` in test files. Standard `with` lifecycle.

Decision events (the `on_tool_call` blocking case from the event-bridge proposal) work by returning a result from the hook: `def on_tool_call(self, event) -> ToolCallEventResult | None`. Returning a result blocks or modifies; returning `None` lets pi proceed.

## Decision points

The author needs to weigh in on these before any implementation starts:

1. **Programming model:** A (stay async), B (facade), C (threads), or D (threads + freethreaded runtime)? Recommendation: **C+D**.
1. **If C/D:** confirm the rewrite-now timing. The cost is bounded (~1 day) but ~10 commits of v5 work get rewritten.
1. **If C/D:** confirm `async def` tool functions are rejected at decoration time (not-pi-2 pattern) rather than silently wrapped.
1. **If D:** confirm we treat 3.14t as a *supported runtime*, not the *default runtime*. The `make install` target stays on 3.11; an additional `make install-nogil` target installs into a parallel `local-py314t.venv/`, matching not-pi-2's pattern. `make test-nogil` runs the same suite on 3.14t.
1. **`Agent` class design** — the `on_*` hooks set, the relationship to the existing `PiPythonHarness`, whether `Agent` subsumes the harness or composes it. **Out of scope for this doc**, but the answer here interacts with C — please confirm Agent design will follow up after concurrency model is settled.

## What this proposal does not contain

- A line-by-line rewrite plan. That follows after the model is chosen.
- A list of `on_*` method names. That belongs to the Agent design, not the concurrency model decision.
- A decision about the event-bridge protocol shape (notify-only vs round-trip vs hybrid). That's in the separate proposal at `dev-notes/2026-05-14-event-bridge-proposal.md` and is largely orthogonal to the threading model.
- Performance benchmarks. None exist for either model in libharness's specific workload; the choice is being made on design fit, not measured throughput.
