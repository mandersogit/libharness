from __future__ import annotations

from pi_python_harness import ToolResult, tool
from pi_python_harness.tools import serve_jsonl


@tool(prompt_snippet="Echo text from Python", prompt_guidelines=["Use py_echo when asked to echo text exactly."])
def py_echo(message: str) -> ToolResult:
    """Echo a message through Python."""
    return ToolResult.text(f"python echo: {message}", details={"message_length": len(message)})


@tool(name="py_add", label="Python Add")
def add(a: int, b: int) -> dict:
    """Add two integers in Python."""
    return {"sum": a + b}


if __name__ == "__main__":
    serve_jsonl()
