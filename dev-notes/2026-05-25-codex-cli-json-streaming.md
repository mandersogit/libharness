---
status: Draft (for review)
created: '2026-05-25'
---

# codex-cli JSON streaming + steering — what we learned, what we could build

## Why this doc exists

During a 2026-05-25 multi-agent adversarial review session, the user asked whether we could enhance our codex-cli integration to (a) see live progress while a review is running and (b) steer codex mid-execution the way `libharness/pi/` drives pi. The answer is yes to both, codex's CLI exposes the necessary surfaces, and we validated the streaming path live. This document captures the findings so a future implementer doesn't have to re-discover.

## The three programmatic surfaces codex-cli exposes

| Surface           | Subcommand                              | Transport   | Maturity     |
| ----------------- | --------------------------------------- | ----------- | ------------ |
| Streaming exec    | `codex exec --json`                     | stdout JSON | stable       |
| MCP server        | `codex mcp-server`                      | stdio MCP   | stable       |
| App / exec server | `codex app-server`, `codex exec-server` | websocket   | experimental |

**Detail:**

- *Streaming exec:* live event stream of the agent's reasoning + tool calls + command outputs. Drop-in for our current `codex exec` invocations — same prompt, same flags, just add `--json` and consume stdout as JSONL. No subprocess persistence; one prompt, one run.
- *MCP server:* long-running codex driven by JSONL requests/responses over MCP protocol. Symmetric with how `libharness.pi.PiRpcClient` drives pi (subprocess + JSONL stdio + per-request semantics). This is the path to use if we ever build a "codex harness" parallel to libharness's pi harness.
- *App / exec server:* same MCP protocol but over a websocket transport. Useful for multi-codex orchestration or for one libharness process driving N remote codex sessions. Protocol schema generatable via `codex app-server generate-json-schema` and `generate-ts` — no reverse-engineering needed.

## Streaming exec — validated end-to-end on 2026-05-25

### Confirmed behavior

`codex exec --json -o <last-message.md> "<prompt>" < /dev/null > <events.jsonl> 2>&1` writes:

- **Event stream to stdout** — incremental JSONL, one event per line, written as codex makes progress. Observed ~120 KB / 20 events per 15-second tick on the medium-effort generalist review we ran.
- **Final markdown to the `-o` file** — the agent's last message (i.e. the review markdown for a review prompt). Works alongside `--json`; the two are not mutually exclusive.

### Event types observed

```text
thread.started               — session begins (carries thread_id)
turn.started                 — model turn begins
turn.completed               — model turn ends
item.started + item.completed — work items, with sub-types:
   command_execution         — codex is about to run / has finished a shell command
                               (carries command, aggregated_output, exit_code, status)
   agent_message             — codex is saying something to the user (carries text)
```

Per event, we can see what codex is thinking, what shell commands it's running, what those commands return, and when turns / threads transition. Everything needed for live diagnostics.

### Caveats (learned the hard way)

1. **Line 1 of the JSONL file is non-JSON.** Codex always prints `Reading additional input from stdin...` to stderr before the event stream starts. With `2>&1` redirect it lands as line 1 of the same file. `jq` will error on it and exit (unless `2>/dev/null` is used — but that hides real errors too). **Correct parser pattern:** `tail -n +2 events.jsonl | jq ...` — skip line 1, then parse cleanly.
1. **Use `< /dev/null` for background invocations.** When launched via `run_in_background=true` or similar, the bash subshell leaves stdin open (not connected to a TTY but not closed either). Codex sees this as "stdin is piped" per `codex exec --help` ("If stdin is piped and a prompt is also provided, stdin is appended as a `<stdin>` block"), prints the cosmetic warning above, then immediately reads EOF and proceeds. Adding `< /dev/null` silences the warning. Codex does **not** actually block on the input in this case — it's purely cosmetic — but the warning is alarming when monitoring a long-running review.
1. **`-o` flag is `--output-last-message`, not "output everything".** It captures only the agent's final reply. For a review prompt where the review IS the final reply, that's what you want. For other prompts where you want the full transcript, parse the JSONL.
1. **`2>/dev/null` is a footgun on `jq`.** During this investigation, the author ran `jq -r '.type' events.jsonl 2>/dev/null | sort -u` to enumerate event types. jq errored on line 1, exited code 5, emitted nothing, and the redirect hid both the error AND the empty output. The investigation only succeeded because the event-type list could also be read from earlier `head` output. The correct invocation: `tail -n +2 events.jsonl | jq -r '.type' | sort -u`. Without `tail`, jq errors stop the pipeline before it processes anything; without `2>/dev/null` hiding the error, the issue is at least visible.

### Recommended invocation pattern

For one-shot non-interactive use (reviews, analysis tasks where we want both streaming visibility and a captured final result):

```bash
codex exec -c model_reasoning_effort="xhigh" --sandbox danger-full-access --json \
  -o /tmp/review-final.md \
  "your prompt" \
  < /dev/null > /tmp/review-events.jsonl 2>&1
```

Live monitor while it runs (separate terminal or background):

```bash
tail -f /tmp/review-events.jsonl | tail -n +2 | jq -r '
  if .type == "item.started" and .item.type == "command_execution"
    then "$ " + (.item.command | tostring)
  elif .type == "item.completed" and .item.type == "agent_message"
    then "🤖 " + (.item.text | .[0:200])
  elif .type == "turn.completed" then "✓ turn done"
  elif .type == "thread.started" then "▶️  thread " + .thread_id
  else empty
  end'
```

Post-hoc summary (count event types in a completed run):

```bash
tail -n +2 /tmp/review-events.jsonl | jq -r '.type' | sort | uniq -c
```

### Side benefit observed

The medium-effort generalist review we ran with `--json` for validation purposes found three real MODERATE bugs the heavyweight reviewers missed (rpc.py `wait_for_event` timeout, `prompt_and_wait` not-request-scoped, `_pi_vendor.fetch_and_extract(force=True)` destroying a working install before verifying the new one). Streaming visibility wasn't the goal there, but the visibility-mode review surfaced different findings than the no-visibility-mode reviews — suggesting `--json` may shift the model's behavior subtly (perhaps because the JSONL emission pacing affects what it chooses to do). Worth keeping an eye on in future use.

## What we could build — MCP-server-based codex harness ("Level 2")

If codex becomes a tool we drive programmatically (not just review-shell-out), the analog of `libharness.pi.PiRpcClient` for codex would be:

```text
libharness/pi/                 libharness/codex/  (hypothetical)
─────────────                  ─────────────────────
PiRpcClient                    CodexMcpClient (spawns `codex mcp-server`, JSONL stdio)
PiLaunchConfig                 CodexLaunchConfig (model, reasoning_effort, sandbox, cwd)
_dispatch_event (pi events)    _dispatch_event (codex MCP events: turn.started, item.*, etc.)
decide_tool_call (bridge)      decide_command_execution (intercept shell commands before exec)
Agent subclass + ClassVars     CodexAgent subclass + per-event-type hooks
```

Reuses the F8/Option A threading infrastructure directly (`HarnessRuntime`, owner-thread queue, `pump_until`).

### What this would enable

- **Mid-run steering** — cancel a turn, inject a follow-up message, redirect when the model goes off-track
- **Tool-call interception** — Python decision hook fires *before* codex runs `rm -rf` (analog of pi's `decide_tool_call`)
- **Multi-codex orchestration** — one libharness Python process driving N concurrent codex sessions
- **Codex-as-tool** — a libharness `Agent` subclass could fire off subagent codex tasks and incorporate their results

### What this would cost

- ~200-500 LOC for the client + agent subclass + tests
- Codex's app-server protocol schema is auto-generatable (`codex app-server generate-json-schema` / `generate-ts`), so no reverse-engineering
- 2-3 sessions of work to reach parity with how libharness drives pi
- Maintenance: codex's protocol versions independently of pi; need to track upstream changes

### Path-of-least-resistance order

1. **Level 1 (immediate, ~5 LOC):** Update the codex-cli skill in `~/.claude/skills/codex-cli/` to recommend `--json` for all review-style invocations. Include the caveats above. Drop the `-o` flag's "captures everything" misreading.
1. **Level 2 (medium, 1-3 sessions):** Build `CodexMcpClient` + `CodexAgent` if and when we want programmatic codex driving.
1. **Level 3 (large, future):** Websocket transport via `app-server` / `exec-server` if we ever need multi-host or multi-codex-session orchestration from one libharness process.

## Open questions

- Does `codex exec --json` shift the model's behavior vs no-JSON exec? The medium-effort review found bugs the xhigh reviews missed — could be effort difference, could be `--json` difference, could be coincidence. Worth a controlled experiment if we care.
- Does `codex mcp-server` support cancellation mid-turn? The MCP protocol has cancellation semantics but codex's server-side handling might be partial. Validate before building Level 2.
- Are there per-event ordering guarantees we can rely on across `turn.started` / `item.started` / `item.completed` / `turn.completed`? Useful for the hook surface design if we build Level 2.

## References

- `~/.claude/skills/codex-cli/SKILL.md` — current skill content (covers `exec` but not `--json` or the server variants)
- `~/.claude/skills/codex-cli/references/cli-reference.md` — flag reference (mentions `--json` briefly)
- `codex exec --help` (output captured 2026-05-25)
- `codex --help` (lists all subcommands including `mcp-server`, `app-server`, `exec-server`)
- Validation run on 2026-05-25: `/tmp/libharness-review/codex-gen-json-{events.jsonl,lastmessage.md}` — 97 events / 321 KB stream + 3596-byte final markdown; medium effort; took ~5 minutes
