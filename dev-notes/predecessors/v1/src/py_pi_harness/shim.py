from __future__ import annotations

from importlib import resources
from pathlib import Path


def default_shim_path() -> Path:
    """Return the packaged TypeScript extension shim path."""

    return Path(str(resources.files("py_pi_harness.shims") / "python_tools_extension.ts"))
