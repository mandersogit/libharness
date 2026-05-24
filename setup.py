"""setup.py — wires the pi-vendor build hook.

Package metadata lives in ``pyproject.toml``; this file exists solely to
install a custom ``build_py`` command that fetches the precompiled pi
binary during wheel build. For sdist installs, that wheel build runs on
the end user's machine, so this hook is the install-time download path.
For binary wheels built in CI (a future opt-in shape — see
``dev-notes/2026-05-24-pi-vendoring-design.md``), the same hook fires
once at wheel-build time and the binary is shipped pre-baked.

Opt-out: set ``LIBHARNESS_SKIP_PI_VENDOR=1`` before running pip; this
suppresses the download and produces a binary-less install. Callers
must then provide their own pi via ``PiLaunchConfig(pi_command=[...])``
or ``LIBHARNESS_PI_PATH``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


class BuildPyWithPi(_build_py):
    def run(self) -> None:
        if os.environ.get("LIBHARNESS_SKIP_PI_VENDOR") == "1":
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


setup(cmdclass={"build_py": BuildPyWithPi})
