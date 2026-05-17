# Threading Model

## Target shape

```text
MainThread
  └─ application code only

PiAsyncioLoop-Thread-2
  ├─ Pi RPC subprocess stdin/stdout readers and writers
  ├─ PythonToolServer socket accept/read/write I/O
  └─ async lifecycle operations submitted by harness owner threads

PiAgentHarness-<id> owner thread, one per normal harness
  ├─ owns PiAgentHarness mutable state
  ├─ owns the PiRpcClient reference
  ├─ owns the PythonToolServer reference
  ├─ writes/generated extension paths
  └─ synchronously marshals async work to PiAsyncioLoop-Thread-2

shared pi-tool_* thread pool
  └─ executes Python tool calls from all harnesses and registries
```

## Ownership rules

- `PiAgentHarness` is a proxy when `threaded=True`.
- `_PiAgentHarnessCore` is created on the owner thread and checks thread affinity on every stateful method.
- A normal harness owner thread is not `MainThread`.
- `threaded=False` is the explicit test exception: the core is created on the current thread.
- The asyncio event loop is never run on `MainThread` by the library.
- Tool registries are configured before `start()` and frozen at `start()`.
- Tool execution is delegated to `HarnessRuntime.tool_executor`, shared by all harnesses using that runtime.

## Why this split exists

Pi RPC and bridge I/O are naturally async. Application embedding usually should not force the application to donate its main thread to an event loop. The dedicated loop thread gives Pi RPC and bridge sockets a stable reactor, while owner threads preserve per-harness state isolation. The shared tool pool prevents one harness from owning all tool worker threads and gives the application one place to control tool concurrency.

## Lifecycle sequence

1. Application creates `HarnessRuntime`.
2. Runtime starts `PiAsyncioLoop-Thread-2` lazily, normally before the first harness owner thread.
3. Application creates one or more `PiAgentHarness` proxies.
4. Each threaded harness creates its owner thread and core after the shared loop exists.
5. `harness.start()` runs on the owner thread.
6. The owner thread freezes its registry.
7. The owner thread submits `PythonToolServer.start()` to the loop thread.
8. The owner thread writes the bridge extension.
9. The owner thread submits `PiRpcClient.start()` to the loop thread.
10. Tool calls enter that harness's bridge server and execute in the shared tool pool.

## Testing posture

Use `threaded=False` for fast single-harness tests that need deterministic access to state from the test thread. The async loop thread and tool pool are still active, so this does not mask RPC/bridge concurrency defects.
