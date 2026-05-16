"""AST contract guard — asserts no async test fns / fixtures / marks.

Replaces the v3 plan's Makefile ``grep`` guard. Runs in the regular pytest
suite, so async-test regressions surface immediately rather than at commit
time. Scoped to the rewritten test files at each phase boundary; phase 4
widens the scope to all of ``tests/`` (when the live tests' ``async def``
patterns are also converted).

**Phase 1 scope** (this file): only the files that exist in the threaded
test infrastructure as of phase 1. The async tests for the still-asyncio
server / rpc / live paths are explicitly out of scope until those files
are rewritten (phases 2, 3, 4 respectively).
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

# Files in the phase-1 scope. Each later phase adds its own files here.
# Phase 1 set: files that exist in the threaded contract from day one.
PHASE_1_SCOPED_FILES: frozenset[str] = frozenset({
    "tests/pi/test_tools.py",
    "tests/pi/test_jsonl.py",  # already sync; included to lock in the guarantee
    "tests/pi/test_runtime_meta.py",
    "tests/pi/test_async_contract_guard.py",
})


# Phase 2: server.py rewrite landed; test_server.py is now sync.
PHASE_2_ADDED_FILES: frozenset[str] = frozenset({
    "tests/pi/test_server.py",
})


# Future phases append to this list; v4 plan § 8 enumerates the per-phase
# additions. The v3-asyncio rpc / live test files (test_rpc_fake.py,
# test_set_model.py, test_real_*.py) stay async until their producing module
# is rewritten.
SCOPED_FILES: frozenset[str] = PHASE_1_SCOPED_FILES | PHASE_2_ADDED_FILES


def _has_async_test_function(tree: ast.AST) -> list[str]:
    """Return names of any async test functions found in the tree."""
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("test_")
    ]


def _has_async_fixture(tree: ast.AST) -> list[str]:
    """Return names of async functions decorated with @pytest.fixture."""
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            # @pytest.fixture, @pytest.fixture(...), @pytest_asyncio.fixture, etc.
            src = ast.unparse(decorator)
            if "fixture" in src:
                out.append(node.name)
                break
    return out


def _has_asyncio_mark(tree: ast.AST) -> list[str]:
    """Return names of functions decorated with @pytest.mark.asyncio."""
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            src = ast.unparse(decorator)
            if "mark.asyncio" in src or "pytest_asyncio" in src:
                out.append(node.name)
                break
    return out


def _has_asyncio_import(tree: ast.AST) -> list[str]:
    """Return statements importing pytest_asyncio (used to enable async tests)."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest_asyncio":
                    out.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest_asyncio":
            out.append(f"from {node.module} import ...")
    return out


def _repo_root() -> Path:
    """Find the repo root from this test file's location."""
    return Path(__file__).resolve().parent.parent.parent


def test_phase_1_scoped_files_have_no_async_tests() -> None:
    """No ``async def test_*`` functions in phase-1-scoped test files."""
    root = _repo_root()
    failures: list[tuple[str, list[str]]] = []
    for rel_path in SCOPED_FILES:
        path = root / rel_path
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(), filename=rel_path)
        async_tests = _has_async_test_function(tree)
        if async_tests:
            failures.append((rel_path, async_tests))
    assert not failures, f"Found async test functions in phase-1 scope: {failures}"


def test_phase_1_scoped_files_have_no_async_fixtures() -> None:
    """No async fixtures in phase-1-scoped test files."""
    root = _repo_root()
    failures: list[tuple[str, list[str]]] = []
    for rel_path in SCOPED_FILES:
        path = root / rel_path
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(), filename=rel_path)
        async_fixtures = _has_async_fixture(tree)
        if async_fixtures:
            failures.append((rel_path, async_fixtures))
    assert not failures, f"Found async fixtures in phase-1 scope: {failures}"


def test_phase_1_scoped_files_have_no_asyncio_marks() -> None:
    """No ``@pytest.mark.asyncio`` decorators in phase-1-scoped test files."""
    root = _repo_root()
    failures: list[tuple[str, list[str]]] = []
    for rel_path in SCOPED_FILES:
        path = root / rel_path
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(), filename=rel_path)
        marks = _has_asyncio_mark(tree)
        if marks:
            failures.append((rel_path, marks))
    assert not failures, f"Found @pytest.mark.asyncio in phase-1 scope: {failures}"


def test_phase_1_scoped_files_have_no_pytest_asyncio_imports() -> None:
    """No ``import pytest_asyncio`` in phase-1-scoped test files."""
    root = _repo_root()
    failures: list[tuple[str, list[str]]] = []
    for rel_path in SCOPED_FILES:
        path = root / rel_path
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(), filename=rel_path)
        imports = _has_asyncio_import(tree)
        if imports:
            failures.append((rel_path, imports))
    assert not failures, f"Found pytest_asyncio imports in phase-1 scope: {failures}"


# ---------------------------------------------------------------------------
# Phase 1 additional tests (per /tmp/phase1-test-plan.md).
# ---------------------------------------------------------------------------


def test_async_contract_guard_scope_is_phase_2() -> None:
    """SCOPED_FILES at phase 2 == phase 1 set + test_server.py.

    Phase 3 adds test_rpc_fake.py + test_set_model.py; phase 4 adds
    test_real_pi_integration.py + test_real_llm.py (when the live tests'
    async patterns are converted to sync).
    """
    expected_in = {
        "tests/pi/test_tools.py",
        "tests/pi/test_jsonl.py",
        "tests/pi/test_runtime_meta.py",
        "tests/pi/test_async_contract_guard.py",
        "tests/pi/test_server.py",
    }
    expected_out = {
        "tests/pi/test_rpc_fake.py",
        "tests/pi/test_set_model.py",
        "tests/pi/test_real_pi_integration.py",
        "tests/pi/test_real_llm.py",
    }
    assert expected_in == SCOPED_FILES, (
        f"Phase 2 SCOPED_FILES mismatch. Got: {sorted(SCOPED_FILES)}"
    )
    for rel in expected_out:
        assert rel not in SCOPED_FILES, (
            f"{rel} should be deferred to its phase; not in phase-2 scope"
        )


def test_async_contract_guard_detector_finds_async_test_function() -> None:
    """The AST detector flags an `async def test_*` correctly (unit-tests the detector)."""
    src = """
async def test_should_be_caught():
    pass

def test_should_not_be_caught():
    pass

async def helper_not_test():
    pass
"""
    tree = ast.parse(src)
    found = _has_async_test_function(tree)
    assert found == ["test_should_be_caught"]


def test_async_contract_guard_detector_finds_async_fixture_and_mark() -> None:
    """The detector flags async fixtures and @pytest.mark.asyncio decorators."""
    fixture_src = """
import pytest

@pytest.fixture
async def my_async_fixture():
    yield

@pytest.fixture
def my_sync_fixture():
    yield
"""
    fixture_tree = ast.parse(fixture_src)
    fixtures_found = _has_async_fixture(fixture_tree)
    assert fixtures_found == ["my_async_fixture"]

    mark_src = """
import pytest

@pytest.mark.asyncio
async def test_marked():
    pass

def test_unmarked():
    pass
"""
    mark_tree = ast.parse(mark_src)
    marks_found = _has_asyncio_mark(mark_tree)
    assert marks_found == ["test_marked"]


def test_async_contract_guard_detector_finds_pytest_asyncio_import() -> None:
    """The detector flags both ``import pytest_asyncio`` and ``from pytest_asyncio import ...``."""
    import_src = """
import pytest_asyncio
from pytest_asyncio import fixture as async_fixture
import pytest  # benign
from pytest import fixture  # benign
"""
    tree = ast.parse(import_src)
    found = _has_asyncio_import(tree)
    assert len(found) == 2
    assert any("import pytest_asyncio" in f for f in found)
    assert any("from pytest_asyncio import" in f for f in found)


def test_pyproject_dev_dependency_includes_pytest_timeout() -> None:
    """pyproject.toml's [project.optional-dependencies].dev includes pytest-timeout."""
    root = _repo_root()
    with (root / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    deps = data["project"]["optional-dependencies"]["dev"]
    matches = [d for d in deps if d.startswith("pytest-timeout")]
    assert len(matches) == 1, f"Expected exactly one pytest-timeout dep, got {matches}"
    # Check it has a >= bound (any version pin form).
    assert ">=" in matches[0] or "==" in matches[0], f"pytest-timeout dep should pin a version: {matches[0]}"
