import * as net from "node:net";
import { randomUUID } from "node:crypto";
import { Type } from "typebox";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type {
  AssistantMessage,
  AssistantMessageEventStream,
  Context,
  Model,
  SimpleStreamOptions,
  TextContent,
  ToolCall,
  Usage,
} from "@earendil-works/pi-ai";
import type { AgentToolResult, ExtensionAPI } from "@earendil-works/pi-coding-agent";

type JsonObject = Record<string, any>;

type PythonToolSpec = {
  name: string;
  label?: string;
  description: string;
  parameters?: JsonObject;
  promptSnippet?: string;
  promptGuidelines?: string[];
  executionMode?: "sequential" | "parallel";
};

const HOST = process.env.PY_PI_TOOLS_HOST ?? "127.0.0.1";
const PORT = Number(process.env.PY_PI_TOOLS_PORT ?? "0");
const TOKEN = process.env.PY_PI_TOOLS_TOKEN ?? "";
const BRIDGE_TIMEOUT_MS = Number(process.env.PY_PI_BRIDGE_TIMEOUT_MS ?? "120000");

function strictSchema(schema: JsonObject | undefined): any {
  return Type.Unsafe(schema ?? { type: "object", properties: {}, additionalProperties: true });
}

function summarizeResult(result: any): string {
  if (!result) return "<no result>";
  const content = Array.isArray(result.content) ? result.content : [];
  const text = content
    .filter((block: any) => block && block.type === "text")
    .map((block: any) => String(block.text ?? ""))
    .join("\n");
  return text || JSON.stringify(result);
}

function normalizeAgentToolResult(value: any): AgentToolResult<any> {
  if (value && Array.isArray(value.content)) {
    return {
      content: value.content,
      details: value.details ?? {},
      ...(value.terminate === undefined ? {} : { terminate: Boolean(value.terminate) }),
    };
  }
  return {
    content: [{ type: "text", text: JSON.stringify(value) }],
    details: value ?? {},
  };
}

async function bridgeCall<T = any>(
  type: string,
  payload: JsonObject = {},
  onUpdate?: (data: any) => void,
  signal?: AbortSignal,
): Promise<T> {
  if (!PORT || !TOKEN) {
    throw new Error("Python bridge is not configured. PY_PI_TOOLS_PORT and PY_PI_TOOLS_TOKEN are required.");
  }

  const id = randomUUID();
  const request = { id, type, token: TOKEN, ...payload };

  return await new Promise<T>((resolve, reject) => {
    const socket = net.createConnection({ host: HOST, port: PORT });
    let buffer = "";
    let settled = false;

    const cleanup = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      socket.removeAllListeners();
      socket.end();
    };

    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      cleanup();
      fn();
    };

    const onAbort = () => finish(() => reject(new Error(`Python bridge call aborted: ${type}`)));
    const timer = setTimeout(() => finish(() => reject(new Error(`Python bridge call timed out: ${type}`))), BRIDGE_TIMEOUT_MS);

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
          finish(() => reject(new Error(`Invalid JSON from Python bridge: ${error instanceof Error ? error.message : error}`)));
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

    socket.on("error", (error) => finish(() => reject(error)));
    socket.on("end", () => {
      if (!settled) finish(() => reject(new Error(`Python bridge ended before response: ${type}`)));
    });
  });
}

function usage(): Usage {
  return {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: 0,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  };
}

function makeBaseMessage(model: Model<any>, stopReason: "stop" | "toolUse"): AssistantMessage {
  return {
    role: "assistant",
    content: [],
    api: "py-pi-fake-stream",
    provider: "py-pi-fake",
    model: model.id,
    usage: usage(),
    stopReason,
    timestamp: Date.now(),
  };
}

function pushText(stream: AssistantMessageEventStream, model: Model<any>, text: string): void {
  const message = makeBaseMessage(model, "stop");
  stream.push({ type: "start", partial: { ...message } });
  message.content = [{ type: "text", text: "" } as TextContent];
  stream.push({ type: "text_start", contentIndex: 0, partial: { ...message, content: [...message.content] } });
  (message.content[0] as TextContent).text = text;
  stream.push({ type: "text_delta", contentIndex: 0, delta: text, partial: { ...message, content: [...message.content] } });
  stream.push({ type: "text_end", contentIndex: 0, content: text, partial: { ...message, content: [...message.content] } });
  stream.push({ type: "done", reason: "stop", message });
  stream.end(message);
}

function pushToolCall(stream: AssistantMessageEventStream, model: Model<any>, toolName: string, args: JsonObject): void {
  const message = makeBaseMessage(model, "toolUse");
  const toolCall: ToolCall = { type: "toolCall", id: `py-fake-${randomUUID()}`, name: toolName, arguments: args };
  stream.push({ type: "start", partial: { ...message } });
  message.content = [{ ...toolCall, arguments: {} }];
  stream.push({ type: "toolcall_start", contentIndex: 0, partial: { ...message, content: [...message.content] } });
  const argText = JSON.stringify(args);
  stream.push({ type: "toolcall_delta", contentIndex: 0, delta: argText, partial: { ...message, content: [...message.content] } });
  message.content = [toolCall];
  stream.push({ type: "toolcall_end", contentIndex: 0, toolCall, partial: { ...message, content: [...message.content] } });
  stream.push({ type: "done", reason: "toolUse", message });
  stream.end(message);
}

function registerFakeProvider(pi: ExtensionAPI, tools: PythonToolSpec[]): void {
  if (process.env.PY_PI_FAKE_PROVIDER !== "1") return;
  const defaultTool = process.env.PY_PI_FAKE_TOOL_NAME || tools[0]?.name;
  const defaultArgs = (() => {
    try {
      return process.env.PY_PI_FAKE_TOOL_ARGS ? JSON.parse(process.env.PY_PI_FAKE_TOOL_ARGS) : {};
    } catch {
      return {};
    }
  })();

  pi.registerProvider("py-pi-fake", {
    name: "Python Pi Fake Provider",
    baseUrl: "http://127.0.0.1/py-pi-fake",
    apiKey: "PY_PI_FAKE_API_KEY",
    api: "py-pi-fake-stream",
    models: [
      {
        id: "toolcaller",
        name: "Python Tool Caller (Fake)",
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 4096,
      },
    ],
    streamSimple: (model: Model<any>, context: Context, options?: SimpleStreamOptions) => {
      const stream = createAssistantMessageEventStream();
      queueMicrotask(async () => {
        try {
          await options?.onResponse?.({ status: 200, headers: {} }, model);
          if (options?.signal?.aborted) {
            const msg = makeBaseMessage(model, "stop");
            msg.stopReason = "aborted" as any;
            msg.errorMessage = "aborted";
            stream.push({ type: "error", reason: "aborted", error: msg });
            stream.end(msg);
            return;
          }
          const toolResults = context.messages.filter((m: any) => m.role === "toolResult");
          if (toolResults.length === 0 && defaultTool) {
            pushToolCall(stream, model, defaultTool, defaultArgs);
          } else {
            const last = toolResults[toolResults.length - 1] as any;
            const text = last ? `Fake provider observed Python tool result for ${last.toolName}:\n${summarizeResult(last)}` : "Fake provider completed without tool use.";
            pushText(stream, model, text);
          }
        } catch (error) {
          const msg = makeBaseMessage(model, "stop");
          msg.stopReason = "error" as any;
          msg.errorMessage = error instanceof Error ? error.message : String(error);
          stream.push({ type: "error", reason: "error", error: msg });
          stream.end(msg);
        }
      });
      return stream;
    },
  });
}

export default async function pythonToolsExtension(pi: ExtensionAPI) {
  const manifest = await bridgeCall<{ tools: PythonToolSpec[] }>("manifest");
  const tools = manifest.tools ?? [];

  for (const spec of tools) {
    pi.registerTool({
      name: spec.name,
      label: spec.label ?? spec.name,
      description: spec.description,
      promptSnippet: spec.promptSnippet,
      promptGuidelines: spec.promptGuidelines,
      parameters: strictSchema(spec.parameters),
      executionMode: spec.executionMode,
      async execute(toolCallId, params, signal, onUpdate) {
        const result = await bridgeCall<any>(
          "execute",
          { tool: spec.name, toolCallId, params },
          (partial) => onUpdate?.(normalizeAgentToolResult(partial)),
          signal,
        );
        return normalizeAgentToolResult(result);
      },
    });
  }

  pi.registerCommand("py-tools", {
    description: "List Python tools registered through py-pi-harness",
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
        ctx.ui.notify(`Invalid JSON args: ${error instanceof Error ? error.message : String(error)}`, "error");
        return;
      }
      try {
        const result = await bridgeCall<any>("execute", { tool: toolName, toolCallId: `manual-${randomUUID()}`, params }, undefined, ctx.signal);
        ctx.ui.notify(summarizeResult(result));
      } catch (error) {
        ctx.ui.notify(`Python tool failed: ${error instanceof Error ? error.message : String(error)}`, "error");
      }
    },
  });

  registerFakeProvider(pi, tools);
}
