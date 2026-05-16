from pathlib import Path

from pi_python_harness.shim import SHIM_TS, write_shim


def test_generated_shim_registers_tools_and_uses_bridge_env(tmp_path: Path):
    target = tmp_path / "bridge.ts"
    write_shim(target)
    text = target.read_text()
    assert text == SHIM_TS
    assert "pi.registerTool" in text
    assert "PI_PY_BRIDGE_PORT" in text
    assert "PI_PY_TOOL_MANIFEST" in text
    assert "callPythonTool" in text
