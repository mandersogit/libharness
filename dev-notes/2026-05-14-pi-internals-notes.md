---
status: Reference
created: '2026-05-14'
---

# Pi internals — findings notes

Reference notes from a co-design session investigating pi's internals. Captures what we verified in the source (with file:line citations) so future sessions don't re-derive these facts. **No decisions in this document.** Decisions live in `docs/DESIGN.md`.

Pi source referenced throughout is reached via `links/pi/` (a symlink to the local pi checkout at `~/git/external/pi/`).

## Bridge transport (loopback TCP vs Unix domain socket)

### What's there

`PythonToolServer` (`src/libharness/pi/server.py:31-185`) is a raw TCP server using `asyncio.start_server` on `127.0.0.1`. Not HTTP. LF-only JSONL framing, one JSONL request per connection.

- **Port:** ephemeral. `port=0` by default; OS picks. Read back via `self._server.sockets[0].getsockname()` (line 55).
- **Lifecycle:** one port per `PiPythonHarness` instance, reused for every tool call during that instance's lifetime. Each tool call opens a fresh TCP connection.
- **Coordinates passed to pi via env vars:** `PI_PY_TOOLS_HOST`, `PI_PY_TOOLS_PORT`, `PI_PY_TOOLS_TOKEN`, `PI_PY_BRIDGE_TIMEOUT_MS` (`server.py:22-28`, consumed in the TS shim at `shim.py:63-66`).
- **Authn:** 32-byte bearer token in every request, compared with `hmac.compare_digest` (`server.py:97`).

### Why TCP and not UDS

The choice was inherited from v5, which inherited it from v1. v1's design doc explicitly lists UDS as a future improvement (`dev-notes/predecessors/v1/docs/DESIGN.md:207-208`). No artifact defended TCP over UDS.

### Why we're keeping TCP for now

The token does the work that matters. Pi state corruption is not the threat — pi internally locks its shared files (see next section), so the bridge token only protects *this harness's pi* from being driven by another process on the box. UDS + `chmod 0600` would be a marginal hardening (same threat, slightly stronger guarantee), not a structural fix.

Specific trade-offs we weighed:

|                         | TCP + token                 | UDS + chmod 0600                |
| ----------------------- | --------------------------- | ------------------------------- |
| Cross-process isolation | bearer-token rejection      | filesystem permission           |
| Per-call latency        | TCP handshake               | meaningfully lower              |
| Path issues             | none                        | macOS 104-char `sun_path` limit |
| Windows                 | works                       | OK on Windows ≥10/1803          |
| Debuggability           | `lsof`, `nc 127.0.0.1 PORT` | `nc -U <path>`                  |
| Cleanup on crash        | TIME_WAIT eventually        | tempdir removal sweeps it       |

The decisive factor: the bearer token already addresses the realistic local-process threat, and Windows support isn't in scope but is trivially preserved.

**Decision recorded in `docs/DESIGN.md`.** Possible future change: revisit UDS if the event bridge surfaces a latency problem we can't fix otherwise.

## Multi-pi concurrency under a shared `HOME`

### Initial wrong claim

In an earlier message I claimed two pi processes sharing `HOME` would race on `settings.json` writes. **That was wrong.** Pi engineers explicitly for this case.

### What pi actually does

Every shared mutable file under `~/.pi/agent/` is lock-protected via `proper-lockfile`:

- `settings.json` — `SettingsManager.withLock` with `lockfile.lockSync` and 10× retry on `ELOCKED` (`packages/coding-agent/src/core/settings-manager.ts:166-220`).
- `auth.json` — `AuthStorage` with both sync (`lockfile.lockSync`) and async (`lockfile.lock`) protection (`packages/coding-agent/src/core/auth-storage.ts:76-152`).
- Session files — each session is its own `<timestamp>_<uuid>.jsonl` in `sessions/<encoded-cwd>/`, so they don't share files at all (`session-manager.ts:748`).
- `keybindings.json`, `models.json` — read-only from pi's perspective; user-edited only (no `writeFile` calls in those modules).

So: **multiple pi processes sharing `HOME` is fully supported**. Interactive multi-pi (the author's stated normal use case) and programmatic multi-harness inherit the same safety story.

### Caveats that remain

These aren't races against pi state — they're about external resources:

1. **OAuth callback port `1455`** during `/login`. Two simultaneous logins would collide. Login is rare and interactive.
1. **Our bridge socket** is already per-harness-isolated (ephemeral port + random token + per-harness generated TS shim in per-harness tempdir). No collision.

## Session structure

### Schema

Every session entry carries a `parentId` (`session-manager.ts:43-48`):

```ts
export interface SessionEntryBase {
    type: string;
    id: string;
    parentId: string | null;
    timestamp: string;
}
```

So sessions are *schematically* trees. The comment at line 136 says so directly: *"Session entry - has id/parentId for tree structure."*

Tree navigation is a public API surface:

- `SessionTreeNode` (lines 152-159) is the recursive node type.
- `getTree()` is in `ReadonlySessionManager` (line 196).
- `BranchSummaryEntry.fromId` (line 79) records branch provenance.
- `CompactionEntry.firstKeptEntryId` (line 69) records compaction boundaries.

Sessions also link to each other — `SessionHeader.parentSession?: string` (line 40) plus `SessionInfo.parentSessionPath?` (line 175). The session collection is a forest; each session is a tree within it.

### How it actually behaves

A typical session JSONL is a **degenerate tree** — i.e., a linear chain. This is because pi's append APIs always parent new entries to the current leaf (`appendMessage` line 837: `parentId: this.leafId`). The tree-capable format is exercised by forking, which doesn't write siblings into the existing file — it writes a new file.

`fork()` (lines 1170-1239):

1. Walk `getBranch(leafId)` to get the linear ancestor path.
1. Create a new session file with a new `sessionId`.
1. Write `[header, ...pathWithoutLabels, ...labelEntries]` into the new file.
1. The header records `parentSession: previousSessionFile` (line 1190).
1. New entries appended after the fork point go in the new file, parented to the copied-leaf entry.

```
Old file:                    New file (after fork from M2):
  header                       header  (parentSession: old)
  M1                           M1      (same id as old)
  M2  ← user forks here        M2      (same id as old)
  M3                           M4      (new, parentId: M2)
                               M5      (new, parentId: M4)
```

So intra-file structure is in practice linear, and inter-file structure is the actual branching tree.

### Order-sensitivity in a session file

`_buildIndex` (lines 753-772) reconstructs structure purely from `parentId` — line order does not determine tree shape. Two consequences:

- You can shuffle most lines in a JSONL session file and the tree it reconstructs is the same.
- BUT `_buildIndex` sets `leafId` to *the last entry in file order*. That's where new appends attach. So order is not "irrelevant for behavior overall" — it's "irrelevant for tree topology, relevant for resume behavior."

### The per-file vs global ID question

Forking copies entries into the new file **with their original IDs** (line 1221: `this.fileEntries = [header, ...pathWithoutLabels, ...]`, which shares the same entry objects). So an entry ID is unique **within a session file**, not globally. If anything in the future wants to identify entries across files (e.g., a python-side sidecar), it must key on `(session_id, entry_id)`, not `entry_id` alone.

### RPC commands that operate on the tree

From `packages/coding-agent/src/modes/rpc/rpc-types.ts`:

- `fork: { entryId }` — fork from any entry id in the current session.
- `clone: {}` — copy the current leaf path into a new session.
- `switch_session: { sessionPath }` — load a different session file (then you can fork from any entry in that loaded session).
- `new_session: { parentSession? }` — start fresh, optionally declaring a parent.
- `get_fork_messages` — list possible fork points from the leaf.
- `get_session_stats`, `export_html`, `set_session_name`, `get_last_assistant_text`, `get_messages` — read operations.

The author's claim ("freely continue a new session from any previous message in any previous session") is realized by the composition: `switch_session` to load any prior session → enumerate its entries via `get_messages` or `get_fork_messages` → `fork(entryId)` to branch from the chosen point. The new session's header records the parent, preserving the cross-file tree.

## Implications captured elsewhere

- **TCP-not-UDS** decision: recorded in `docs/DESIGN.md`.
- **Use pi-native sessions** as a first-class feature (don't invent a Python-side replica): recorded in `docs/DESIGN.md`.
- **Per-file (not global) entry ID uniqueness**: noted here as a future-gotcha if anyone builds a sidecar keyed by entry id.
