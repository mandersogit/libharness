---
status: Decided
created: '2026-05-15'
---

# Specialist (chat-only) models as libharness tools, not pi-core models

## Decision

OpenRouter models that do not support tool-use (Hermes, Cydonia, Euryale, and the broader class of community uncensored/abliterated/roleplay finetunes) will be exposed to pi as **libharness Python tools**, not registered in `models.json` as pi-core models.

`models.json` and `settings.json:enabledModels` will contain **only tool-capable** entries: as of 2026-05-15 that is `openai-codex/gpt-5.5` (default) and the three Mistral entries (medium-3-5, small-2603, large-2512). Specialists access goes through a yet-to-be-written `consult(...)`-style tool registered by libharness.

## Context

Empirically tested 2026-05-15:

| Model                          | Tool-use support? | Free-spirited? (offensive-joke prompt)                       |
| ------------------------------ | ----------------- | ------------------------------------------------------------ |
| `gpt-5.5` (openai-codex)       | yes               | n/a (default; not probed here)                               |
| `mistralai/mistral-medium-3-5` | yes               | no — "I can't help with that"                                |
| `mistralai/mistral-small-2603` | yes               | no — verbose lecture refusal                                 |
| `mistralai/mistral-large-2512` | yes               | no — "I won't tell offensive jokes"                          |
| `x-ai/grok-4.3`                | yes               | yes — full compliance (but expensive: ~$1.25/$2.49 per Mtok) |
| `x-ai/grok-4.1-fast`           | yes               | yes — but **DEPRECATED upstream** (xAI 404)                  |
| `x-ai/grok-4-fast`             | yes               | n/a — **DEPRECATED upstream**                                |
| `nousresearch/hermes-4-70b`    | **no**            | yes (mild) — pun-format compliance                           |
| `nousresearch/hermes-4-405b`   | **no**            | no — hard refusal (more guarded than its 70b sibling)        |
| `thedrummer/cydonia-24b-v4.1`  | **no**            | yes — mild gender-targeted pun                               |
| `sao10k/l3.3-euryale-70b`      | **no**            | yes (strong) — slur, no preamble                             |

**Detail:**

- *Tool-use detection:* OpenRouter returns HTTP 404 with `"No endpoints found that support tool use. Try disabling 'read'."` when a request carrying `tools[]` lands on a model whose hosted endpoints don't advertise tool-calling. Pi's `--no-tools` / `-nt` flag bypasses by dropping the tool list — fine for ad-hoc chat, not viable for the agent loop, which needs tool-calling to do anything useful.
- *Cost note:* Grok 4.3 is currently the only registry candidate that is both tool-capable *and* content-permissive. At ~$1.25 in / $2.49 out per Mtok it is ~10× the price of the lost `grok-4.1-fast`. Nothing on OpenRouter currently fills the "cheap + free-spirited + tool-capable" niche grok-fast vacated.

## Rationale

Two reasons:

1. **Architectural fit.** Per `CLAUDE.md`: *"Python owns tool authoring, orchestration, and lifecycle."* A specialist model wrapped as a Python tool is exactly the library's mandate. Registering a chat-only model in `models.json` and hoping the agent loop won't try to use tools is fighting the abstraction.
1. **Composability and cost.** Orchestrator/specialist separation matches AutoGen-style dual-capability patterns. A tool-capable brain (gpt-5.5, Mistral, Grok 4.3, or future Ollama-on-Behemoth) drives the agent loop; a Python tool wraps the specialist's HTTP call when uncensored/creative output is wanted. The specialist pays for raw chat tokens only — no tool-definition overhead.

## What to implement (later)

Not now. The asyncio→threads rewrite is the active milestone and the tool registry is one of the modules changing. Implement the specialist-tool wrapper **alongside or after** that rewrite, with the post-rewrite sync `def` tool signature.

Sketch of what it will look like:

```python
@registry.register(description="Consult an OpenRouter chat-only specialist model. Use for offensive humor, uncensored creative writing, or other content the default model refuses.")
def consult_uncensored(model: str, prompt: str) -> str:
    # Make OpenRouter HTTP call (or shell out to `pi -nt`) and return the assistant text.
    ...
```

Open design questions to resolve at implementation time:

- **Granularity:** one polymorphic `consult(model, prompt)` tool, vs several model-specific tools with rich descriptions so the agent picks well (`tell_offensive_joke`, `creative_uncensored_writing`, …). Polymorphic is simpler but agents often pick poorly between models without hand-holding; per-specialty is more verbose but steers the agent.
- **Transport:** direct OpenRouter HTTP (simpler, lower latency, requires duplicating auth and base-URL knowledge) vs sub-pi subprocess pinned with `-nt` (consistent with pi's config plumbing, slower, heavier). Probably direct HTTP for v1; sub-pi if there are auth-plumbing or telemetry reasons to share pi's pipes.
- **Cost guard:** specialists are cheaper than core, but a runaway agent could still rack up calls. The tool should return token counts in its result so the agent's traces are auditable.

## Related

- `project-specialist-models-as-tools` (memory)
- `project-libharness-ask-gap` (memory) — same rewrite milestone is the natural home for both a `libharness.ask()` helper and this tool wrapper.
- `dev-notes/2026-05-15-concurrency-model-discussion.md` — the rewrite this work depends on.
- `dev-notes/SESSION-STATE.md` § Pending tasks — the asyncio→threads rewrite is item #1.
