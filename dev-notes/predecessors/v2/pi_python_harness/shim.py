from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence


def _json_array(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def write_python_tool_shim(
    path: str | Path,
    *,
    tool_server_command: Sequence[str],
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    command_name: str = "python-tools",
) -> Path:
    """Write a minimal TypeScript extension that exposes Python tools to Pi.

    The generated extension is deliberately generic. It contains no per-tool
    business logic; all Python tool definitions and executions are delegated to
    a Python JSONL tool server.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    extension = TS_SHIM_TEMPLATE.replace("__TOOL_SERVER_COMMAND__", _json_array(tool_server_command))
    extension = extension.replace("__TOOL_SERVER_CWD__", json.dumps(str(cwd) if cwd is not None else None))
    extension = extension.replace("__TOOL_SERVER_ENV__", json.dumps(env or {}, ensure_ascii=False))
    extension = extension.replace("__COMMAND_NAME__", json.dumps(command_name))
    path.write_text(extension, encoding="utf-8")
    return path


def write_faux_toolcall_provider_extension(
    path: str | Path,
    *,
    tool_name: str,
    arguments: dict,
    provider: str = "pyharness-test",
    model_id: str = "pyharness-faux-1",
    final_text: str = "done",
) -> Path:
    """Write a test-only Pi extension that uses Pi's faux provider.

    The provider first asks the model to call `tool_name` with `arguments`, then
    returns `final_text`. This lets integration tests exercise tool execution
    without any external LLM.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    extension = TS_FAUX_PROVIDER_TEMPLATE.replace("__TOOL_NAME__", json.dumps(tool_name))
    extension = extension.replace("__TOOL_ARGUMENTS__", json.dumps(arguments, ensure_ascii=False))
    extension = extension.replace("__PROVIDER__", json.dumps(provider))
    extension = extension.replace("__MODEL_ID__", json.dumps(model_id))
    extension = extension.replace("__FINAL_TEXT__", json.dumps(final_text))
    path.write_text(extension, encoding="utf-8")
    return path


TS_SHIM_TEMPLATE = r'''
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type JsonObject = Record<string, unknown>;
type Pending = { resolve: (value: JsonObject) => void; reject: (error: Error) => void };

const TOOL_SERVER_COMMAND = __TOOL_SERVER_COMMAND__ as string[];
const TOOL_SERVER_CWD = __TOOL_SERVER_CWD__ as string | null;
const TOOL_SERVER_ENV = __TOOL_SERVER_ENV__ as Record<string, string>;
const COMMAND_NAME = __COMMAND_NAME__ as string;

class PythonToolBridge {
  private child: ChildProcessWithoutNullStreams | undefined;
  private pending = new Map<string, Pending>();
  private requestId = 0;
  private stderr = "";
  private buffer = "";

  start(): void {
    if (this.child) return;
    const [cmd, ...args] = TOOL_SERVER_COMMAND;
    if (!cmd) throw new Error("Python tool server command is empty");
    this.child = spawn(cmd, args, {
      cwd: TOOL_SERVER_CWD ?? undefined,
      env: { ...process.env, ...TOOL_SERVER_ENV },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child.stdout.on("data", (chunk: Buffer | string) => this.onStdout(chunk.toString("utf8")));
    this.child.stderr.on("data", (chunk: Buffer | string) => { this.stderr += chunk.toString("utf8"); });
    this.child.on("exit", (code, signal) => {
      const error = new Error(`Python tool server exited code=${code} signal=${signal}. stderr=${this.stderr}`);
      for (const item of this.pending.values()) item.reject(error);
      this.pending.clear();
      this.child = undefined;
    });
  }

  stop(): void {
    this.child?.kill("SIGTERM");
    this.child = undefined;
  }

  async listTools(): Promise<JsonObject[]> {
    const response = await this.request({ method: "list_tools" });
    const tools = response.tools;
    if (!Array.isArray(tools)) throw new Error("Python tool server returned invalid tools list");
    return tools as JsonObject[];
  }

  async callTool(name: string, params: JsonObject, toolCallId: string, cwd: string): Promise<JsonObject> {
    const response = await this.request({ method: "call_tool", name, params, toolCallId, cwd });
    const result = response.result;
    if (!result || typeof result !== "object") throw new Error(`Python tool ${name} returned invalid result`);
    return result as JsonObject;
  }

  private request(payload: JsonObject): Promise<JsonObject> {
    this.start();
    const id = `ts_${++this.requestId}`;
    const fullPayload = { ...payload, id };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Timed out waiting for Python tool server response to ${String(payload.method)}. stderr=${this.stderr}`));
      }, 30000);
      this.pending.set(id, {
        resolve: (value) => { clearTimeout(timer); resolve(value); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      });
      this.child!.stdin.write(`${JSON.stringify(fullPayload)}\n`);
    });
  }

  private onStdout(chunk: string): void {
    this.buffer += chunk;
    while (true) {
      const index = this.buffer.indexOf("\n");
      if (index < 0) return;
      let line = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      if (!line) continue;
      let message: JsonObject;
      try {
        message = JSON.parse(line) as JsonObject;
      } catch (error) {
        continue;
      }
      const id = String(message.id ?? "");
      const pending = this.pending.get(id);
      if (!pending) continue;
      this.pending.delete(id);
      if (message.ok === false) {
        pending.reject(new Error(String(message.error ?? "Python tool server error")));
      } else {
        pending.resolve(message);
      }
    }
  }
}

function asStringArray(value: unknown): string[] | undefined {
  return Array.isArray(value) && value.every((x) => typeof x === "string") ? value : undefined;
}

export default async function pythonToolShim(pi: ExtensionAPI) {
  const bridge = new PythonToolBridge();
  const tools = await bridge.listTools();

  for (const item of tools) {
    const name = String(item.name ?? "");
    if (!name) throw new Error("Python tool has no name");
    const label = String(item.label ?? name);
    const description = String(item.description ?? label);
    const parameters = (item.parameters && typeof item.parameters === "object" ? item.parameters : { type: "object", properties: {} }) as never;
    const promptSnippet = typeof item.promptSnippet === "string" ? item.promptSnippet : undefined;
    const promptGuidelines = asStringArray(item.promptGuidelines);
    const executionMode = item.executionMode === "sequential" || item.executionMode === "parallel" ? item.executionMode : undefined;

    pi.registerTool({
      name,
      label,
      description,
      parameters,
      promptSnippet,
      promptGuidelines,
      executionMode,
      async execute(toolCallId, params, _signal, onUpdate, ctx) {
        const result = await bridge.callTool(name, params as JsonObject, toolCallId, ctx.cwd);
        const updates = Array.isArray(result.updates) ? result.updates : [];
        for (const update of updates) onUpdate?.(update);
        return {
          content: Array.isArray(result.content) ? result.content as never : [{ type: "text", text: JSON.stringify(result) }],
          details: result.details ?? {},
          terminate: typeof result.terminate === "boolean" ? result.terminate : undefined,
        };
      },
    });
  }

  pi.registerCommand(COMMAND_NAME, {
    description: "List tools registered through the Python bridge",
    handler: async (_args, ctx) => {
      const latest = await bridge.listTools();
      ctx.ui.notify(`Python tools: ${latest.map((t) => String(t.name)).join(", ") || "none"}`, "info");
    },
  });

  pi.on("session_shutdown", () => {
    bridge.stop();
  });
}
'''


TS_FAUX_PROVIDER_TEMPLATE = r'''
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
    fauxAssistantMessage(fauxToolCall(__TOOL_NAME__, __TOOL_ARGUMENTS__), { stopReason: "toolUse" }),
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
'''
