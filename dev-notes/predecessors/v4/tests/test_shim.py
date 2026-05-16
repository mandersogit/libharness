from pathlib import Path

from pi_python_harness import ToolRegistry, write_generated_shim


def test_generated_shim_contains_no_per_tool_typescript(tmp_path: Path):
    registry = ToolRegistry()

    @registry.tool(name="echo_text")
    def echo_text(text: str) -> str:
        return text

    generated = write_generated_shim(registry, bridge_url="http://127.0.0.1:12345", token="secret", directory=tmp_path)
    manifest = generated.manifest_path.read_text()
    shim = generated.extension_path.read_text()
    assert "echo_text" in manifest
    assert "pi.registerTool" in shim
    assert "executeViaPythonBridge" in shim
    assert generated.env["PI_PY_TOOL_BRIDGE_TOKEN"] == "secret"
