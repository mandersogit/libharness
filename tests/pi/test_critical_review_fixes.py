"""Regression tests for the three CRITICAL bugs from the 2026-05-25 adversarial review.

- **C1** — `close()` strands pending `_HookCall` / `_Continuation` items.
  Fixed by close-coordinating the enqueue in `_dispatch_sync_hook` and
  `_register_continuation` under `_close_lock`. When closed, the awaiting
  coroutine / future is unblocked with an exception instead of stranding.
- **C2** — sync hook on owner thread calling public harness API deadlocks
  the queue. Fixed by detecting re-entry in `submit()` and raising
  `RuntimeError` instead.
- **C3** — `python -m build` produces a `py3-none-any` wheel with a
  host-specific binary. Fixed by overriding `bdist_wheel` to tag the
  wheel with the host platform when the binary is bundled.

See `dev-notes/2026-05-25-adversarial-review-synthesis.md` for the full
finding details.
"""

from __future__ import annotations

import concurrent.futures
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from tests.pi.test_threaded_agent import fake_config

from libharness.pi import Agent, AgentEvent, HarnessRuntime, PiAgentHarness, ToolRegistry

# ---------------------------------------------------------------------------
# C1: close-during-pending dispatch must not strand awaiting futures.
# ---------------------------------------------------------------------------


def test_c1_dispatch_sync_hook_after_close_completes_with_exception() -> None:
    """A `_HookCall` enqueued by `_dispatch_sync_hook` AFTER `close()`
    has put `_STOP` would otherwise sit behind `_STOP` and strand the
    loop coroutine awaiting `wrap_future(completion)`. The fix takes
    `_close_lock` and completes the future with `RuntimeError` if
    closed."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-c1-hook")

    class Probe(Agent):
        def on_agent_start(self, event: AgentEvent) -> None:
            pass  # never actually fires; we drive _dispatch_sync_hook directly

    agent = Probe(ToolRegistry(), config=fake_config(), runtime=runtime)
    try:
        agent.start()
        agent.close()
        # After close, dispatching a sync hook MUST not hang and MUST raise.
        with pytest.raises(RuntimeError, match="closed; sync hook"):
            runtime.run_async(
                agent._dispatch_sync_hook(lambda: None, event_name="agent_start"),
                timeout=2.0,
            )
    finally:
        runtime.close()


def test_c1_register_continuation_after_close_completes_main_future() -> None:
    """When close() has fired, an in-flight loop_future whose
    done-callback runs AFTER the owner thread has consumed `_STOP`
    should complete its main_future with an exception instead of
    silently stranding."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-c1-cont")
    agent = PiAgentHarness(ToolRegistry(), config=fake_config(), runtime=runtime)
    try:
        agent.start()
        # Inflight loop_future + main_future, register continuation NOW
        # (while agent is open), then close(), then resolve the loop_future.
        loop_future: concurrent.futures.Future[str] = concurrent.futures.Future()
        main_future: concurrent.futures.Future[str] = concurrent.futures.Future()
        agent._register_continuation(loop_future, main_future)
        agent.close()
        # Now resolve the loop_future. The done-callback fires; with C1's
        # close-coordination it sees _closed=True and completes
        # main_future with RuntimeError instead of stranding.
        loop_future.set_result("would-be-result")
        with pytest.raises(RuntimeError, match="closed; continuation"):
            main_future.result(timeout=2.0)
    finally:
        runtime.close()


def test_c1_main_future_unblocks_when_continuation_arrives_after_stop() -> None:
    """End-to-end: an operation submitted before close() that returns a
    loop_future, where the loop_future resolves AFTER close() puts
    _STOP. The submitting caller (main_future) must unblock — either
    with the result (if the continuation slipped in before _STOP) or
    with a RuntimeError (if it lost the race). Critically, it must
    NOT hang."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-c1-e2e")
    agent = PiAgentHarness(ToolRegistry(), config=fake_config(), runtime=runtime)
    try:
        agent.start()
        # Submit an operation that returns a manually-controlled loop_future.
        external_future: concurrent.futures.Future[str] = concurrent.futures.Future()

        def op(core: Any) -> concurrent.futures.Future[str]:
            return external_future

        main = agent.submit(op)
        # Wait briefly for the owner thread to dispatch op and register
        # the continuation.
        time.sleep(0.05)
        # Close the agent; this puts _STOP on the queue.
        agent.close()
        # Now resolve the loop_future. The done-callback fires, sees
        # _closed=True, completes main with RuntimeError. (If it slipped
        # in before _STOP, main resolves normally — either is acceptable;
        # the bug is hanging.)
        external_future.set_result("ok")
        try:
            result = main.result(timeout=2.0)
            assert result == "ok"
        except RuntimeError as exc:
            assert "closed" in str(exc)
        except concurrent.futures.TimeoutError:
            pytest.fail("main future hung; C1 fix not effective")
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# C2: sync hook on owner thread calling public harness API must raise,
# not deadlock the queue.
# ---------------------------------------------------------------------------


def test_c2_sync_hook_calling_snapshot_raises_clear_error() -> None:
    """In threaded=True, a sync hook running on the owner thread that
    calls a public harness method (which goes through `submit()`)
    would otherwise enqueue work onto the same queue it's currently
    consuming and deadlock. The fix detects re-entry by thread ident
    and raises RuntimeError with an actionable message."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-c2")

    captured_error: list[BaseException] = []

    class ReentrantAgent(Agent):
        def on_agent_start(self, event: AgentEvent) -> None:
            # This call from the owner thread should raise immediately.
            try:
                self.snapshot()
            except BaseException as exc:
                captured_error.append(exc)
                raise

    agent = ReentrantAgent(ToolRegistry(), config=fake_config(), runtime=runtime)
    try:
        agent.start()
        # Drive the sync hook via the loop. The hook calls self.snapshot()
        # which should raise RuntimeError, NOT hang.
        try:
            runtime.run_async(
                agent._async_on_event({"type": "agent_start"}),
                timeout=2.0,
            )
        except concurrent.futures.TimeoutError:
            pytest.fail(
                "sync hook calling public API hung; C2 re-entry guard not effective"
            )
        # The hook's snapshot() call raised; the agent_class dispatcher
        # caught the exception and logged it (notification hooks fail-open).
        # We captured it before the dispatcher swallowed it.
        assert len(captured_error) == 1, (
            f"expected exactly one captured RuntimeError; got {captured_error}"
        )
        msg = str(captured_error[0])
        assert isinstance(captured_error[0], RuntimeError)
        assert "owner thread" in msg
        assert "deadlock" in msg
        assert "async_*" in msg or "async hook" in msg
    finally:
        agent.close()
        runtime.close()


def test_c2_threaded_false_allows_reentry() -> None:
    """In threaded=False, MainThread runs both the pump and the hook,
    and the pump is re-entrant (a hook can call public API which starts
    a nested pump). C2's guard is owner-thread-ident-based and only
    fires in threaded=True. Confirm threaded=False keeps working."""
    runtime = HarnessRuntime(loop_thread_name="PiAsyncioLoop-c2-nf")
    snapshot_results: list[Any] = []

    class ReentrantAgent(Agent):
        def on_agent_start(self, event: AgentEvent) -> None:
            snapshot_results.append(self.snapshot())

    agent = ReentrantAgent(
        ToolRegistry(), config=fake_config(), runtime=runtime, threaded=False
    )
    try:
        agent.start()
        agent.pump_until(agent._async_on_event({"type": "agent_start"}))
        assert len(snapshot_results) == 1
        assert snapshot_results[0].started is True
    finally:
        agent.close()
        runtime.close()


# ---------------------------------------------------------------------------
# C3: wheel built with the bundled pi binary must have a platform tag,
# not py3-none-any. The corresponding sdist install path is independently
# tested via test_precompiled_pi_binary.py + the existing pi-vendoring tests.
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _build_module_available() -> bool:
    # Check `python -m build` is runnable — i.e., `build.__main__` exists.
    # `import build` alone is NOT enough: the project root may contain a
    # `build/` directory (from prior wheel builds) that Python treats as
    # a namespace package, making `import build` succeed against an empty
    # namespace.
    try:
        import build.__main__  # noqa: F401

        return True
    except ImportError:
        return False


_skip_if_no_build = pytest.mark.skipif(
    not _build_module_available(),
    reason="`build` package not installed in this venv (C3 tests need pip's build frontend)",
)


@pytest.fixture
def _isolated_build_dir(tmp_path: Path) -> Path:
    """Clean output dir for `python -m build --wheel`."""
    return tmp_path / "wheel-out"


@_skip_if_no_build
def test_c3_wheel_with_bundled_binary_has_platform_tag(
    _isolated_build_dir: Path,
) -> None:
    """When the pi binary IS present in src/libharness/_vendor/pi/, the
    built wheel filename must end with a platform tag (e.g.
    `linux_aarch64`), not `any`. Otherwise the wheel claims universal
    compatibility while shipping a host-specific binary.

    This test depends on the project having `src/libharness/_vendor/pi/pi`
    already in place (i.e. `make install-pi` was run on this host).
    Skipped otherwise.
    """
    project = _project_root()
    vendored = project / "src" / "libharness" / "_vendor" / "pi" / "pi"
    if not vendored.exists():
        pytest.skip(f"vendored binary not present at {vendored}; run `make install-pi`")

    _isolated_build_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(_isolated_build_dir), str(project)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"build failed: {result.stderr}"
    wheels = list(_isolated_build_dir.glob("libharness-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel; got {wheels}"
    name = wheels[0].name
    assert not name.endswith("-none-any.whl"), (
        f"wheel built with bundled binary should not be tagged any; got {name}"
    )
    # The platform tag is anything except 'any'; we don't assert a specific
    # value because the test must work on any host (linux_aarch64,
    # linux_x86_64, darwin_arm64, etc.).


@_skip_if_no_build
def test_c3_wheel_without_binary_has_universal_tag(
    _isolated_build_dir: Path,
) -> None:
    """When the binary is NOT present (SKIP env var AND clean vendor
    dir), the wheel is genuinely pure-Python and `py3-none-any` is the
    correct tag. We achieve "clean vendor dir" via a temporary
    work-tree copy with the vendor dir removed."""
    project = _project_root()
    work = _isolated_build_dir.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    # Copy the project minus the vendor dir + build artifacts.
    for item in project.iterdir():
        if item.name in {"build", "dist", "local.venv", "local-ft.venv", ".sandbox", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "experiments", "node_modules"}:
            continue
        target = work / item.name
        if item.is_dir():
            shutil.copytree(item, target, symlinks=False, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, target)
    # Remove the vendored binary if it was copied.
    vendored_in_work = work / "src" / "libharness" / "_vendor"
    if vendored_in_work.exists():
        shutil.rmtree(vendored_in_work)

    _isolated_build_dir.mkdir(parents=True, exist_ok=True)
    env = {**__import__("os").environ, "LIBHARNESS_SKIP_PI_VENDOR": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(_isolated_build_dir), str(work)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, f"build failed: {result.stderr}"
    wheels = list(_isolated_build_dir.glob("libharness-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel; got {wheels}"
    name = wheels[0].name
    assert name.endswith("-py3-none-any.whl"), (
        f"wheel built without bundled binary should be tagged py3-none-any; got {name}"
    )
