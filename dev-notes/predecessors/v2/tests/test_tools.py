from __future__ import annotations

import io
import json
import unittest

from pi_python_harness.tools import ToolRegistry, ToolResult, handle_request, serve_jsonl


class ToolRegistryTests(unittest.TestCase):
    def test_registers_schema_from_type_hints_and_calls_tool(self) -> None:
        registry = ToolRegistry()

        @registry.register
        def greet(name: str, excited: bool = False) -> ToolResult:
            return ToolResult.text(("Hello " + name + ("!" if excited else ".")))

        tools = registry.list_tools()
        self.assertEqual(tools[0]["name"], "greet")
        self.assertIn("name", tools[0]["parameters"]["required"])
        result = registry.call("greet", {"name": "Pi", "excited": True})
        self.assertEqual(result["content"][0]["text"], "Hello Pi!")

    def test_jsonl_server_uses_lf_only_records(self) -> None:
        registry = ToolRegistry()

        @registry.register
        def echo(text: str) -> str:
            return text

        # U+2028 is valid inside a JSON string and must not split the record.
        request = {"id": "1", "method": "call_tool", "name": "echo", "params": {"text": "a\u2028b"}}
        stdin = io.StringIO(json.dumps(request, ensure_ascii=False) + "\n")
        stdout = io.StringIO()
        serve_jsonl(registry, stdin=stdin, stdout=stdout)
        response = json.loads(stdout.getvalue())
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["content"][0]["text"], "a\u2028b")

    def test_handle_request_lists_tools(self) -> None:
        registry = ToolRegistry()

        @registry.register(name="answer")
        def answer() -> int:
            return 42

        response = handle_request(registry, {"id": "x", "method": "list_tools"})
        self.assertEqual(response["id"], "x")
        self.assertEqual(response["tools"][0]["name"], "answer")


if __name__ == "__main__":
    unittest.main()
