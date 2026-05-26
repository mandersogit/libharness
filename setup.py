"""setup.py — wires the pi-vendor build hook + platform-correct wheel tag.

Package metadata lives in ``pyproject.toml``; this file exists solely to
install two custom build commands:

- ``build_py`` (``BuildPyWithPi``): fetches the precompiled pi binary
  during wheel build. For sdist installs, that wheel build runs on the
  end user's machine, so this hook is the install-time download path.
- ``bdist_wheel`` (``BdistWheelWithPiTag``): tags the resulting wheel
  with the host platform when the binary is bundled. C3 fix: prevents
  ``python -m build`` from producing a misleading ``py3-none-any``
  universal wheel that actually contains a host-specific binary. With
  this override, the wheel filename ends in ``...linux_x86_64.whl`` (or
  whatever the host is), so pip will refuse to install it on
  wrong-platform machines and will fall through to sdist (which
  re-runs the vendor step for the user's own platform).

Opt-out: set ``LIBHARNESS_SKIP_PI_VENDOR=1`` before running pip; this
suppresses the download AND keeps the wheel tagged ``py3-none-any``
(a genuinely pure-Python wheel without the binary). Callers must then
provide their own pi via ``PiLaunchConfig(pi_command=[...])`` or
``LIBHARNESS_PI_PATH`` at runtime.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.bdist_wheel import bdist_wheel as _bdist_wheel


def _skip_vendoring() -> bool:
    return os.environ.get("LIBHARNESS_SKIP_PI_VENDOR") == "1"


def _vendored_binary_present() -> bool:
    """True iff a pi executable is sitting in ``src/libharness/_vendor/pi/``.

    Drives the wheel-tag decision (C3): if a binary IS present (regardless
    of whether we just fetched it or it was already there from an earlier
    run), the wheel will contain it and must be platform-tagged. If a
    binary is NOT present (e.g. ``LIBHARNESS_SKIP_PI_VENDOR=1`` was set
    AND the dir was clean), the wheel is genuinely pure-Python.
    """
    vendor_root = Path(__file__).resolve().parent / "src" / "libharness" / "_vendor" / "pi"
    return (vendor_root / "pi").exists() or (vendor_root / "pi.exe").exists()


class BuildPyWithPi(_build_py):
    def run(self) -> None:
        if _skip_vendoring():
            print(
                "[libharness setup.py] LIBHARNESS_SKIP_PI_VENDOR=1; "
                "skipping pi binary fetch — set LIBHARNESS_PI_PATH at runtime "
                "or pass PiLaunchConfig(pi_command=[...]) explicitly.",
                flush=True,
            )
            super().run()
            return

        src_path = Path(__file__).resolve().parent / "src"
        sys.path.insert(0, str(src_path))
        try:
            from libharness._pi_vendor import PiVendorError, fetch_and_extract
        finally:
            sys.path.pop(0)

        try:
            fetch_and_extract()
        except PiVendorError as exc:
            raise RuntimeError(
                f"libharness install failed during pi vendor step: {exc}\n\n"
                f"Either fix the underlying issue (network/firewall/sha256), "
                f"or set LIBHARNESS_SKIP_PI_VENDOR=1 to install without the "
                f"vendored binary; you will then need to provide your own pi "
                f"via PiLaunchConfig(pi_command=[...]) or LIBHARNESS_PI_PATH."
            ) from exc

        super().run()


class BdistWheelWithPiTag(_bdist_wheel):
    """C3 fix: tag wheels with the host platform when the pi binary is bundled.

    Without this override, ``python -m build`` produces a
    ``libharness-X.Y.Z-py3-none-any.whl`` that contains the host's
    platform-specific pi executable. Publishing such a wheel to PyPI
    makes pip happy to install it on every platform — but the binary
    inside only works on one. Windows users get "no pi.exe found";
    wrong-arch Unix users hit "Exec format error".

    With the override, the wheel filename is platform-tagged like
    ``libharness-X.Y.Z-py3-none-linux_x86_64.whl``. pip won't install
    it on a wrong-platform machine; instead it falls through to the
    sdist, which on-the-fly re-runs the vendor step for the user's
    correct platform.

    The ``py3``/``none`` parts of the tag are intentional: pi's binary
    is architecturally independent of Python implementation and ABI;
    only the OS+arch matters.

    When ``LIBHARNESS_SKIP_PI_VENDOR=1`` is set, the binary is not
    bundled, so the wheel IS pure-Python and the default
    ``py3-none-any`` tag is correct.
    """

    def finalize_options(self) -> None:  # type: ignore[override]
        super().finalize_options()
        # Decision is on actual binary presence (post-BuildPyWithPi), NOT
        # on the SKIP env var alone. A stale ``src/libharness/_vendor/pi/``
        # from a prior local build would otherwise sneak a binary into a
        # supposedly-pure wheel.
        if _vendored_binary_present():
            self.root_is_pure = False

    def get_tag(self):  # type: ignore[override]
        impl, abi, plat = super().get_tag()
        if not _vendored_binary_present():
            return impl, abi, plat  # genuine pure-Python wheel: py3-none-any
        # Bundled binary: it doesn't care about Python impl or ABI,
        # but it DOES care about OS+arch. Override impl/abi to the
        # universal ``py3`` / ``none`` while preserving the platform tag.
        return "py3", "none", plat


setup(
    cmdclass={
        "build_py": BuildPyWithPi,
        "bdist_wheel": BdistWheelWithPiTag,
    }
)
