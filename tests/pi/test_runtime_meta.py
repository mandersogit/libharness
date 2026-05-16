"""Runtime-meta sanity test.

Records the actual runtime identity to a known artifact path so CI can verify
``make test-311`` and ``make test-ft`` provably ran on different interpreters.
``sys._is_gil_enabled()`` is only present on CPython 3.13+; the guard makes the
test safe on 3.11 (where the attribute is absent and the GIL is always on).

The artifact path is namespaced by venv tag so the two lanes write to distinct
files; ``make threads-runtime-meta-check`` (added in phase 4) will compare them.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

import pytest


def _runtime_tag() -> str:
    """Derive a short tag for the artifact filename.

    Prefer the LIBHARNESS_VENV env var (set by scripts/test.sh when running
    via ``LIBHARNESS_VENV=...``); fall back to the active interpreter prefix.
    """
    venv = os.environ.get("LIBHARNESS_VENV", "")
    if venv:
        # e.g. /home/manderso/git/github/libharness/local-ft.venv → "local-ft"
        name = Path(venv).name
        return name.removesuffix(".venv") or name
    # Fallback: distinguish 3.11 vs 3.14t by sys.executable basename.
    return Path(sys.executable).parent.parent.name


def test_runtime_is_what_we_think() -> None:
    """Record runtime identity to ``.pytest-runtime-meta/<tag>.json``.

    This test never fails on its own; it only writes a sentinel artifact.
    CI (``make threads-runtime-meta-check``, phase 4) consumes the artifacts
    from both venvs and asserts they disagree on ``_is_gil_enabled()``.
    """
    artifact_dir = Path(".pytest-runtime-meta")
    artifact_dir.mkdir(exist_ok=True)
    gil_enabled = getattr(sys, "_is_gil_enabled", lambda: True)()
    artifact = {
        "tag": _runtime_tag(),
        "version_info": [sys.version_info.major, sys.version_info.minor, sys.version_info.micro],
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
        "gil_enabled": gil_enabled,
    }
    out = artifact_dir / f"{_runtime_tag()}.json"
    out.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    # Sanity assertion: the artifact we just wrote round-trips.
    loaded = json.loads(out.read_text())
    assert loaded["version_info"][0] == 3
    assert loaded["version_info"][1] >= 11
    assert loaded["implementation"] == "CPython"
    # GIL-enabled flag matches the active interpreter:
    # - 3.11 / 3.12 / 3.13: sys._is_gil_enabled attr absent → fallback True.
    # - 3.13+ with GIL on: True.
    # - 3.13t / 3.14t: False.
    assert isinstance(loaded["gil_enabled"], bool)


def test_runtime_meta_uses_libharness_venv_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``LIBHARNESS_VENV`` env var controls the artifact tag, so the two venv lanes don't clobber each other."""
    monkeypatch.setenv("LIBHARNESS_VENV", "/some/path/local-ft.venv")
    assert _runtime_tag() == "local-ft"
    monkeypatch.setenv("LIBHARNESS_VENV", "/some/path/local.venv")
    assert _runtime_tag() == "local"


def test_runtime_meta_writes_expected_artifact_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Artifact has the documented shape (tag, version_info, implementation, executable, gil_enabled)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIBHARNESS_VENV", "/tmp/test.venv")
    test_runtime_is_what_we_think()
    artifact_file = tmp_path / ".pytest-runtime-meta" / "test.json"
    assert artifact_file.exists()
    payload = json.loads(artifact_file.read_text())
    assert payload["tag"] == "test"
    assert payload["implementation"] == "CPython"
    assert isinstance(payload["version_info"], list) and len(payload["version_info"]) == 3
    assert isinstance(payload["executable"], str)
    assert isinstance(payload["gil_enabled"], bool)
