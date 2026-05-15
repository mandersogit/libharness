from __future__ import annotations

from typing import Literal

import pytest

from libharness.pi.tools import (
    MANIFEST_PROTOCOL_VERSION,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
)


def test_registry_infers_schema_and_context() -> None:
    registry = ToolRegistry()

    @registry.register(description="Search things")
    def search(query: str, limit: int = 5, mode: Literal["fast", "deep"] = "fast", ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult.text(f"{query}:{limit}:{mode}:{ctx is not None}")

    manifest = registry.manifest()
    assert manifest["protocolVersion"] == MANIFEST_PROTOCOL_VERSION == 1
    tool = manifest["tools"][0]
    assert tool["name"] == "search"
    assert tool["parameters"]["properties"]["query"]["type"] == "string"
    assert tool["parameters"]["properties"]["limit"]["type"] == "integer"
    assert tool["parameters"]["properties"]["mode"]["enum"] == ["fast", "deep"]
    assert "query" in tool["parameters"]["required"]
    assert "ctx" not in tool["parameters"]["properties"]


def test_duplicate_names_are_rejected() -> None:
    registry = ToolRegistry()

    @registry.register(name="same")
    def one() -> str:
        return "1"

    with pytest.raises(ToolError):
        @registry.register(name="same")
        def two() -> str:
            return "2"
