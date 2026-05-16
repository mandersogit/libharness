#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


state = {
    "model": None,
    "thinkingLevel": "off",
    "isStreaming": False,
    "isCompacting": False,
    "steeringMode": "all",
    "followUpMode": "one-at-a-time",
    "sessionId": "fake-session",
    "autoCompactionEnabled": False,
    "messageCount": 0,
    "pendingMessageCount": 0,
}
messages = []

for raw in sys.stdin:
    try:
        cmd = json.loads(raw)
    except Exception as exc:
        out({"type": "response", "command": "parse", "success": False, "error": str(exc)})
        continue
    if cmd.get("type") == "get_state":
        out({"id": cmd.get("id"), "type": "response", "command": "get_state", "success": True, "data": state})
    elif cmd.get("type") == "get_messages":
        out({"id": cmd.get("id"), "type": "response", "command": "get_messages", "success": True, "data": {"messages": messages}})
    elif cmd.get("type") == "prompt":
        message = cmd.get("message", "")
        state["messageCount"] += 1
        messages.append({"role": "user", "content": [{"type": "text", "text": message}], "timestamp": int(time.time() * 1000)})
        out({"id": cmd.get("id"), "type": "response", "command": "prompt", "success": True})
        out({"type": "agent_start"})
        out({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "hello"}})
        out({"type": "agent_end"})
    elif cmd.get("type") == "extension_ui_response":
        out({"type": "extension_ui_ack", "id": cmd.get("id")})
    else:
        out({"id": cmd.get("id"), "type": "response", "command": cmd.get("type", "unknown"), "success": False, "error": "unknown command"})
