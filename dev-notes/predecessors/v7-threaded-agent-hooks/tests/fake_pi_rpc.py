from __future__ import annotations

import json
import sys


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for raw in sys.stdin:
    if not raw.strip():
        continue
    request = json.loads(raw)
    typ = request.get("type")
    if typ == "get_state":
        send(
            {
                "type": "extension_ui_request",
                "id": "ui-1",
                "method": "confirm",
                "message": "continue?",
            }
        )
        response = json.loads(sys.stdin.readline())
        if (
            response.get("type") != "extension_ui_response"
            or response.get("confirmed") is not False
        ):
            send(
                {
                    "type": "response",
                    "id": request.get("id"),
                    "success": False,
                    "error": "bad UI response",
                }
            )
        else:
            send(
                {
                    "type": "response",
                    "id": request.get("id"),
                    "success": True,
                    "data": {"sessionId": "fake", "isStreaming": False},
                }
            )
    elif typ == "prompt":
        send({"type": "response", "id": request.get("id"), "success": True, "data": {}})
        send({"type": "agent_start"})
        send(
            {
                "type": "message_start",
                "message": {"role": "user", "content": request.get("message")},
            }
        )
        send(
            {"type": "message_end", "message": {"role": "user", "content": request.get("message")}}
        )
        send({"type": "agent_end", "messages": []})
    elif typ == "set_model":
        if "modelId" not in request or "model" in request:
            send(
                {
                    "type": "response",
                    "id": request.get("id"),
                    "success": False,
                    "error": "expected modelId",
                }
            )
        else:
            send(
                {
                    "type": "response",
                    "id": request.get("id"),
                    "success": True,
                    "data": {"provider": request.get("provider"), "id": request.get("modelId")},
                }
            )
    else:
        send({"type": "response", "id": request.get("id"), "success": True, "data": {}})
