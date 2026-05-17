"""Generated TypeScript extensions for Pi."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_bridge_shim(path: str | Path, *, diagnostic_commands: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = PRODUCTION_TS_SHIM.replace(
        "__DIAGNOSTIC_COMMANDS__", "true" if diagnostic_commands else "false"
    )
    path.write_text(text, encoding="utf-8")
    return path


def write_faux_toolcall_provider_extension(
    path: str | Path,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    provider: str = "pyharness-test",
    model_id: str = "pyharness-faux-1",
    final_text: str = "done",
) -> Path:
    """Write a test-only faux provider extension.

    The faux provider first emits a tool call, then emits a final assistant
    message. This exercises Pi's real tool loop without requiring an LLM API key.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = TS_FAUX_PROVIDER_TEMPLATE
    text = text.replace("__TOOL_NAME__", json.dumps(tool_name, ensure_ascii=False))
    text = text.replace("__TOOL_ARGUMENTS__", json.dumps(arguments, ensure_ascii=False))
    text = text.replace("__PROVIDER__", json.dumps(provider, ensure_ascii=False))
    text = text.replace("__MODEL_ID__", json.dumps(model_id, ensure_ascii=False))
    text = text.replace("__FINAL_TEXT__", json.dumps(final_text, ensure_ascii=False))
    path.write_text(text, encoding="utf-8")
    return path


PRODUCTION_TS_SHIM = r"""
import * as net from "node:net";
import { randomUUID } from "node:crypto";
import { Type } from "typebox";
import type { AgentToolResult, ExtensionAPI } from "@earendil-works/pi-coding-agent";

type JsonObject = Record<string, any>;

type PythonBridgeManifest = {
  protocolVersion: 1;
  tools: PythonToolSpec[];
  initialOpenGates?: string[];
  decisionTimeoutsMs?: Record<string, number>;
};

type PythonToolSpec = {
  name: string;
  label?: string;
  description: string;
  parameters?: JsonObject;
  promptSnippet?: string;
  promptGuidelines?: string[];
  executionMode?: "sequential" | "parallel";
};

const HOST = process.env.PI_PY_TOOLS_HOST ?? "127.0.0.1";
const PORT = Number(process.env.PI_PY_TOOLS_PORT ?? "0");
const TOKEN = process.env.PI_PY_TOOLS_TOKEN ?? "";
const BRIDGE_TIMEOUT_MS = Number(process.env.PI_PY_BRIDGE_TIMEOUT_MS ?? "120000");
const DIAGNOSTIC_COMMANDS =
  __DIAGNOSTIC_COMMANDS__ && process.env.PI_PY_DIAGNOSTIC_COMMANDS !== "0";

const DECISION_EVENTS = [
  "resources_discover",
  "session_start",
  "session_before_switch",
  "session_before_fork",
  "session_before_compact",
  "session_compact",
  "session_shutdown",
  "session_before_tree",
  "session_tree",
  "context",
  "before_provider_request",
  "after_provider_response",
  "before_agent_start",
  "model_select",
  "thinking_level_select",
  "tool_call",
  "tool_result",
  "user_bash",
  "input",
] as const;

function strictSchema(schema: JsonObject | undefined): any {
  return Type.Unsafe(schema ?? { type: "object", properties: {}, additionalProperties: false });
}

function normalizeAgentToolResult(value: any): AgentToolResult<any> {
  if (value && Array.isArray(value.content)) {
    return {
      content: value.content,
      details: value.details ?? {},
      ...(value.terminate === undefined ? {} : { terminate: Boolean(value.terminate) }),
    };
  }
  if (typeof value === "string") {
    return { content: [{ type: "text", text: value }], details: {} };
  }
  return {
    content: [{ type: "text", text: JSON.stringify(value) }],
    details: value && typeof value === "object" ? value : { value },
  };
}

function summarizeResult(value: any): string {
  const result = normalizeAgentToolResult(value);
  const text = result.content
    .filter((block: any) => block && block.type === "text")
    .map((block: any) => String(block.text ?? ""))
    .join("\n");
  return text || JSON.stringify(result);
}

function jsonSafe(value: any): any {
  try {
    return JSON.parse(JSON.stringify(value, (_key, item) => {
      if (typeof item === "bigint") return item.toString();
      if (typeof item === "function") return undefined;
      if (item instanceof AbortSignal) return { aborted: item.aborted };
      return item;
    }));
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return { type: String(value?.type ?? "unknown"), serializationError: detail };
  }
}

async function bridgeCall<T = any>(
  type: string,
  payload: JsonObject = {},
  onUpdate?: (data: any) => void,
  signal?: AbortSignal,
  timeoutMs: number | null = BRIDGE_TIMEOUT_MS,
): Promise<T> {
  if (!PORT || !TOKEN) {
    throw new Error(
      "Python bridge is not configured. " +
        "PI_PY_TOOLS_PORT and PI_PY_TOOLS_TOKEN are required.",
    );
  }

  const id = randomUUID();
  const request = { id, type, token: TOKEN, ...payload };

  return await new Promise<T>((resolve, reject) => {
    const socket = net.createConnection({ host: HOST, port: PORT });
    let buffer = "";
    let settled = false;
    let timer: NodeJS.Timeout | undefined;

    const cleanup = (destroy = false) => {
      if (timer) clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      socket.removeAllListeners();
      if (destroy) socket.destroy();
      else socket.end();
    };

    const finish = (fn: () => void, destroy = false) => {
      if (settled) return;
      settled = true;
      cleanup(destroy);
      fn();
    };

    const onAbort = () =>
      finish(() => reject(new Error(`Python bridge call aborted: ${type}`)), true);
    if (timeoutMs !== null && timeoutMs !== undefined) {
      timer = setTimeout(
        () => finish(() => reject(new Error(`Python bridge call timed out: ${type}`)), true),
        timeoutMs,
      );
    }

    signal?.addEventListener("abort", onAbort, { once: true });

    socket.on("connect", () => {
      socket.write(`${JSON.stringify(request)}\n`, "utf8");
    });

    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      while (true) {
        const index = buffer.indexOf("\n");
        if (index < 0) break;
        let line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        if (line.endsWith("\r")) line = line.slice(0, -1);
        if (!line) continue;

        let frame: any;
        try {
          frame = JSON.parse(line);
        } catch (error) {
          const detail = error instanceof Error ? error.message : error;
          finish(() => reject(new Error(`Invalid JSON from Python bridge: ${detail}`)), true);
          return;
        }
        if (frame.id !== id) continue;

        if (frame.type === "update") {
          onUpdate?.(frame.data);
          continue;
        }

        if (frame.type === "response") {
          if (frame.success) {
            finish(() => resolve(frame.data as T));
          } else {
            const err = new Error(String(frame.error ?? "Python bridge call failed"));
            (err as any).bridgeData = frame.data;
            finish(() => reject(err));
          }
          return;
        }
      }
    });

    socket.on("error", (error) => finish(() => reject(error), true));
    socket.on("end", () => {
      if (!settled) finish(() => reject(new Error(`Python bridge ended before response: ${type}`)));
    });
  });
}

function bridgeNotify(type: string, payload: JsonObject = {}): Promise<void> {
  if (!PORT || !TOKEN) return Promise.resolve();
  const id = randomUUID();
  const request = { id, type, token: TOKEN, ...payload };
  return new Promise<void>((resolve, reject) => {
    const socket = net.createConnection({ host: HOST, port: PORT });
    let settled = false;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      socket.removeAllListeners();
      fn();
    };
    socket.on("connect", () => {
      socket.end(`${JSON.stringify(request)}\n`, "utf8");
      finish(resolve);
    });
    socket.on("error", (error) => finish(() => reject(error)));
  });
}

async function handleDecisionEvent(
  eventName: string,
  event: any,
  openGates: Set<string>,
  decisionTimeoutsMs: Record<string, number>,
  signal?: AbortSignal,
): Promise<any> {
  const data = jsonSafe(event);
  if (!data.type) data.type = eventName;

  if (!openGates.has(eventName)) {
    void bridgeNotify("notify_event", { event: eventName, data }).catch((error) => {
      const detail = error instanceof Error ? error.message : String(error);
      console.warn(`[pi-python-harness] notify_event ${eventName} failed: ${detail}`);
    });
    return undefined;
  }

  const timeout = Object.prototype.hasOwnProperty.call(decisionTimeoutsMs, eventName)
    ? decisionTimeoutsMs[eventName]
    : null;
  try {
    const result = await bridgeCall<any>(
      "event",
      { event: eventName, data },
      undefined,
      signal,
      timeout,
    );
    return result === null ? undefined : result;
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    if (timeout !== null && detail.includes("timed out")) {
      console.warn(`[pi-python-harness] decision hook timed out for ${eventName}: ${detail}`);
    } else {
      console.warn(`[pi-python-harness] decision hook failed for ${eventName}: ${detail}`);
    }
    return undefined;
  }
}

export default async function pythonToolsExtension(pi: ExtensionAPI) {
  const manifest = await bridgeCall<PythonBridgeManifest>("manifest");
  if (manifest.protocolVersion !== 1) {
    throw new Error(`Unsupported Python bridge protocol version: ${manifest.protocolVersion}`);
  }
  const tools = manifest.tools ?? [];
  const openGates = new Set(manifest.initialOpenGates ?? []);
  const decisionTimeoutsMs = manifest.decisionTimeoutsMs ?? {};

  for (const eventName of DECISION_EVENTS) {
    (pi.on as any)(eventName, async (event: any, ctx: any) => {
      return await handleDecisionEvent(
        eventName,
        event,
        openGates,
        decisionTimeoutsMs,
        ctx?.signal,
      );
    });
  }

  for (const spec of tools) {
    pi.registerTool({
      name: spec.name,
      label: spec.label ?? spec.name,
      description: spec.description,
      promptSnippet: spec.promptSnippet,
      promptGuidelines: spec.promptGuidelines,
      parameters: strictSchema(spec.parameters),
      executionMode: spec.executionMode,
      async execute(toolCallId, params, signal, onUpdate, ctx) {
        const result = await bridgeCall<any>(
          "execute",
          {
            tool: spec.name,
            toolCallId,
            params: params ?? {},
            cwd: ctx?.cwd,
            context: {
              model: ctx?.model,
              hasUI: Boolean(ctx?.ui),
            },
          },
          (partial) => onUpdate?.(normalizeAgentToolResult(partial)),
          signal,
          BRIDGE_TIMEOUT_MS,
        );
        return normalizeAgentToolResult(result);
      },
    });
  }

  if (DIAGNOSTIC_COMMANDS) {
    pi.registerCommand("py-tools", {
      description: "List Python tools registered through pi-python-harness",
      async handler(_args, ctx) {
        ctx.ui.notify(`Python tools: ${tools.map((tool) => tool.name).join(", ") || "<none>"}`);
      },
    });

    pi.registerCommand("py-tool", {
      description: "Execute a Python tool directly: /py-tool <name> {json args}",
      async handler(args, ctx) {
        const trimmed = args.trim();
        const space = trimmed.indexOf(" ");
        const toolName = space < 0 ? trimmed : trimmed.slice(0, space);
        const rawArgs = space < 0 ? "{}" : trimmed.slice(space + 1).trim();
        if (!toolName) {
          ctx.ui.notify("Usage: /py-tool <name> {json args}", "warning");
          return;
        }
        let params: JsonObject;
        try {
          params = rawArgs ? JSON.parse(rawArgs) : {};
        } catch (error) {
          const detail = error instanceof Error ? error.message : String(error);
          ctx.ui.notify(`Invalid JSON args: ${detail}`, "error");
          return;
        }
        try {
          const result = await bridgeCall<any>(
            "execute",
            { tool: toolName, toolCallId: `manual-${randomUUID()}`, params },
            undefined,
            ctx.signal,
            BRIDGE_TIMEOUT_MS,
          );
          ctx.ui.notify(summarizeResult(result));
        } catch (error) {
          const detail = error instanceof Error ? error.message : String(error);
          ctx.ui.notify(`Python tool failed: ${detail}`, "error");
        }
      },
    });
  }
}
"""

TS_FAUX_PROVIDER_TEMPLATE = r"""
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fauxAssistantMessage, fauxToolCall, registerFauxProvider } from "@earendil-works/pi-ai";

export default function fauxToolcallProvider(pi: ExtensionAPI) {
  const providerName = __PROVIDER__ as string;
  const modelId = __MODEL_ID__ as string;
  const faux = registerFauxProvider({
    provider: providerName,
    api: `${providerName}-api`,
    models: [{
      id: modelId,
      name: "Python Harness Faux Model",
      reasoning: false,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 32000,
      maxTokens: 4096,
    }],
  });
  faux.setResponses([
    fauxAssistantMessage(
      fauxToolCall(__TOOL_NAME__, __TOOL_ARGUMENTS__),
      { stopReason: "toolUse" },
    ),
    fauxAssistantMessage(__FINAL_TEXT__),
  ]);
  pi.registerProvider(providerName, {
    baseUrl: "http://localhost:0",
    apiKey: "PYHARNESS_FAUX_API_KEY",
    api: faux.api as never,
    models: faux.models.map((model) => ({
      id: model.id,
      name: model.name,
      reasoning: model.reasoning,
      input: model.input,
      cost: model.cost,
      contextWindow: model.contextWindow,
      maxTokens: model.maxTokens,
    })),
  });
}
"""
