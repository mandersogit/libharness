"""Generate the minimal TypeScript extension used to bridge Pi tools to Python."""

from __future__ import annotations

from pathlib import Path


SHIM_TS = r'''
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { readFileSync } from "node:fs";
import { createConnection, Socket } from "node:net";

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`Missing required environment variable ${name}`);
  return value;
}

const manifestPath = requireEnv("PI_PY_TOOL_MANIFEST");
const bridgeHost = process.env.PI_PY_BRIDGE_HOST ?? "127.0.0.1";
const bridgePort = Number(requireEnv("PI_PY_BRIDGE_PORT"));
const bridgeToken = requireEnv("PI_PY_BRIDGE_TOKEN");

type PythonToolManifest = {
  protocolVersion: number;
  tools: Array<{
    name: string;
    label?: string;
    description: string;
    parameters: Record<string, unknown>;
    promptSnippet?: string;
    promptGuidelines?: string[];
    executionMode?: "parallel" | "sequential";
  }>;
};

type ToolWireMessage =
  | { type: "tool_update"; id?: string; partialResult: unknown }
  | { type: "tool_result"; id?: string; result: unknown }
  | { type: "tool_error"; id?: string; error: string; traceback?: string };

function sendJsonLine(socket: Socket, value: unknown): void {
  socket.write(`${JSON.stringify(value)}\n`, "utf8");
}

function callPythonTool(
  toolName: string,
  toolCallId: string,
  args: unknown,
  signal: AbortSignal | undefined,
  onUpdate: ((partial: any) => void) | undefined,
): Promise<any> {
  return new Promise((resolve, reject) => {
    const socket = createConnection({ host: bridgeHost, port: bridgePort });
    const requestId = `${toolName}:${toolCallId}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
    let buffer = "";
    let settled = false;

    function cleanup(): void {
      signal?.removeEventListener("abort", onAbort);
      socket.removeAllListeners();
      if (!socket.destroyed) socket.destroy();
    }

    function finishError(error: Error): void {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    }

    function finishValue(value: any): void {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    }

    function onAbort(): void {
      finishError(new Error(`Python tool ${toolName} aborted`));
    }

    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) {
      onAbort();
      return;
    }

    socket.on("connect", () => {
      sendJsonLine(socket, {
        type: "call_tool",
        id: requestId,
        token: bridgeToken,
        toolName,
        toolCallId,
        args,
      });
    });

    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      while (true) {
        const idx = buffer.indexOf("\n");
        if (idx === -1) break;
        let line = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 1);
        if (line.endsWith("\r")) line = line.slice(0, -1);
        if (!line) continue;
        let message: ToolWireMessage;
        try {
          message = JSON.parse(line) as ToolWireMessage;
        } catch (error) {
          finishError(error instanceof Error ? error : new Error(String(error)));
          return;
        }
        if (message.type === "tool_update") {
          onUpdate?.(message.partialResult);
        } else if (message.type === "tool_result") {
          finishValue(message.result);
        } else if (message.type === "tool_error") {
          const suffix = message.traceback ? `\n${message.traceback}` : "";
          finishError(new Error(`${message.error}${suffix}`));
        }
      }
    });

    socket.on("error", (error) => finishError(error));
    socket.on("end", () => {
      if (!settled) finishError(new Error(`Python bridge closed before ${toolName} returned`));
    });
    socket.on("close", () => {
      if (!settled) finishError(new Error(`Python bridge closed before ${toolName} returned`));
    });
  });
}

export default async function(pi: ExtensionAPI) {
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as PythonToolManifest;
  if (manifest.protocolVersion !== 1) {
    throw new Error(`Unsupported Python bridge manifest protocol version: ${manifest.protocolVersion}`);
  }

  for (const tool of manifest.tools) {
    pi.registerTool({
      name: tool.name,
      label: tool.label ?? tool.name,
      description: tool.description,
      promptSnippet: tool.promptSnippet,
      promptGuidelines: tool.promptGuidelines,
      parameters: tool.parameters as any,
      executionMode: tool.executionMode,
      async execute(toolCallId, params, signal, onUpdate) {
        return await callPythonTool(tool.name, toolCallId, params, signal, onUpdate);
      },
    });
  }
}
'''.lstrip()


def write_shim(path: str | Path) -> Path:
    """Write the generated TypeScript bridge extension."""

    target = Path(path)
    target.write_text(SHIM_TS, encoding="utf-8")
    return target
