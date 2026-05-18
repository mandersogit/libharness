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
            # Echo `received: request` so the v5 test_set_model.py regression
            # test can assert on the wire shape that arrived. Pi itself returns
            # a different shape, but the test cares about what the *client*
            # sent — which means the fake must echo it back.
            send(
                {
                    "type": "response",
                    "id": request.get("id"),
                    "success": True,
                    "data": {
                        "provider": request.get("provider"),
                        "id": request.get("modelId"),
                        "received": request,
                    },
                }
            )
    elif typ in {
        "fork",
        "clone",
        "switch_session",
        "get_session_stats",
        "export_html",
        "set_session_name",
        "get_fork_messages",
    }:
        # Echo the request back under `received` so test_session_wrappers.py
        # can assert on the wire shape the client sent (same pattern as
        # `set_model` above — pi's real responses differ per command but the
        # tests pin what the *client* serialized).
        send(
            {
                "type": "response",
                "id": request.get("id"),
                "success": True,
                "data": {"received": request},
            }
        )
    else:
        send({"type": "response", "id": request.get("id"), "success": True, "data": {}})
