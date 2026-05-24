"""Pi binary vendor: download, sha256-verify, extract; runtime resolver.

Canonical fetch + resolver logic, shared between three call sites:

- ``setup.py``'s ``build_py`` hook — runs at sdist install (on the user's
  machine) or wheel build (in CI). Drops the binary into
  ``src/libharness/_vendor/pi/`` before standard wheel packaging.
- ``make install-pi`` — runs ``python -m libharness._pi_vendor install``
  to populate the editable-install location during dev setup.
- ``PiLaunchConfig`` — calls ``resolve_pi_command()`` when no explicit
  ``pi_command`` was supplied, picking the vendored binary or surfacing
  a loud diagnostic.

Distribution decision (2026-05-24): install-time fetch is the libharness
default; a pre-baked wheel variant is a future opt-in. The runtime
resolver explicitly does NOT fall back to ``$PATH`` — silent install
failures that masquerade as "working pi from somewhere" would produce
wrong-version behavior that's hard to debug. See
``dev-notes/2026-05-24-pi-vendoring-design.md``.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Sequence
from pathlib import Path

PI_VERSION = "0.75.5"

_PLATFORM_HASHES: dict[str, tuple[str, str]] = {
    "darwin-arm64":  ("pi-darwin-arm64.tar.gz",  "eb8f039c41e87b1431c82baa97f807c4d391b77d66ecb41e096270ae36f9362d"),
    "darwin-x64":    ("pi-darwin-x64.tar.gz",    "2556c5166b495b8380ae50b59ed61cf2e6c4d7d231c0df0ed214d9de49f47dac"),
    "linux-arm64":   ("pi-linux-arm64.tar.gz",   "8b5c66fc3baee5fea4077f62307f792d6293660315b3ce4d0940c49353b45f50"),
    "linux-x64":     ("pi-linux-x64.tar.gz",     "ac687acd72705dabe967fd40d365317f68054878be1cb2c0ae9a0592fd108b5d"),
    "windows-arm64": ("pi-windows-arm64.zip",    "d35a202cc683b7e9b93a9c8aa2414f736d0cd2b7fdf2f2885e28f895ce28ced9"),
    "windows-x64":   ("pi-windows-x64.zip",      "775bec6cf05ed13cfb866cf13280c7878d018ccf9db11b44902c80589166cbb5"),
}

_DOWNLOAD_BASE = "https://github.com/earendil-works/pi/releases/download"


class PiVendorError(RuntimeError):
    """A fetch/verify/extract or resolver step failed in a user-actionable way."""


def detect_platform() -> str:
    """Return the platform tag (e.g. ``'linux-arm64'``) for this host.

    Raises ``PiVendorError`` on unsupported OS/arch combinations.
    """
    sys_name = platform.system().lower()
    machine = platform.machine().lower()

    if sys_name == "linux":
        os_tag = "linux"
    elif sys_name == "darwin":
        os_tag = "darwin"
    elif sys_name == "windows":
        os_tag = "windows"
    else:
        raise PiVendorError(f"unsupported OS for pi vendoring: {sys_name!r}")

    if machine in ("x86_64", "amd64"):
        arch_tag = "x64"
    elif machine in ("arm64", "aarch64"):
        arch_tag = "arm64"
    else:
        raise PiVendorError(
            f"unsupported architecture for pi vendoring: {machine!r} on {sys_name}"
        )

    tag = f"{os_tag}-{arch_tag}"
    if tag not in _PLATFORM_HASHES:
        raise PiVendorError(
            f"no pi binary published for platform {tag!r} at v{PI_VERSION}"
        )
    return tag


def vendored_pi_dir(*, package_root: Path | None = None) -> Path:
    """Where the vendored ``pi/`` directory should live.

    ``package_root`` defaults to the directory containing this module file —
    that's ``src/libharness/`` for editable installs and
    ``site-packages/libharness/`` for wheel installs, by design.
    """
    if package_root is None:
        package_root = Path(__file__).resolve().parent
    return package_root / "_vendor" / "pi"


def vendored_pi_binary(*, package_root: Path | None = None) -> Path:
    """Where the pi executable should live inside the vendored dir."""
    exe = "pi.exe" if sys.platform == "win32" else "pi"
    return vendored_pi_dir(package_root=package_root) / exe


def _download_to(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url) as response, dest.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(response, out)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_and_extract(*, package_root: Path | None = None, force: bool = False) -> Path:
    """Download, sha256-verify, extract pi for the current platform.

    Idempotent: if the binary already exists at the expected path and
    ``force`` is False, returns immediately without re-downloading. Set
    ``force=True`` (or ``LIBHARNESS_PI_FORCE_REFETCH=1``) to redo the
    download regardless.

    Returns the path to the installed pi binary.
    """
    plat = detect_platform()
    asset_name, expected_sha = _PLATFORM_HASHES[plat]

    target_binary = vendored_pi_binary(package_root=package_root)
    force = force or os.environ.get("LIBHARNESS_PI_FORCE_REFETCH") == "1"
    if target_binary.exists() and not force:
        return target_binary

    download_base = os.environ.get("LIBHARNESS_PI_DOWNLOAD_BASE") or _DOWNLOAD_BASE
    url = f"{download_base}/v{PI_VERSION}/{asset_name}"

    pi_dir = vendored_pi_dir(package_root=package_root)
    pi_dir.parent.mkdir(parents=True, exist_ok=True)
    if pi_dir.exists():
        shutil.rmtree(pi_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        archive = Path(tmpdir) / asset_name
        print(
            f"[libharness._pi_vendor] downloading {url} ({asset_name})",
            file=sys.stderr,
        )
        _download_to(url, archive)
        actual = _sha256_of(archive)
        if actual != expected_sha:
            raise PiVendorError(
                f"sha256 mismatch for {asset_name}: "
                f"expected {expected_sha}, got {actual}"
            )
        print(
            f"[libharness._pi_vendor] sha256 verified ({expected_sha[:16]}…)",
            file=sys.stderr,
        )
        extract_to = pi_dir.parent
        if asset_name.endswith(".tar.gz"):
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(extract_to)  # noqa: S202
        elif asset_name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_to)
        else:
            raise PiVendorError(f"don't know how to extract {asset_name}")
        if target_binary.exists():
            target_binary.chmod(target_binary.stat().st_mode | 0o111)

    print(
        f"[libharness._pi_vendor] installed pi {PI_VERSION} at {target_binary}",
        file=sys.stderr,
    )
    return target_binary


def resolve_pi_command(
    *,
    explicit: str | Sequence[str] | None = None,
    package_root: Path | None = None,
) -> list[str]:
    """Resolve which pi to invoke, per the documented runtime order.

    Order:

    1. ``explicit`` argument (e.g. ``PiLaunchConfig.pi_command`` if set).
    2. ``LIBHARNESS_PI_PATH`` env var.
    3. Vendored at ``<package_root>/_vendor/pi/pi``.
    4. Raise ``PiVendorError`` with a diagnostic pointing at (1) and (2).

    There is intentionally no fallback to ``$PATH`` — silent install
    failures masquerading as "working pi from somewhere" produce
    wrong-version behavior that's hard to debug; legitimate "bring your
    own pi" cases go through (1) or (2).
    """
    if explicit is not None:
        if isinstance(explicit, str):
            return [explicit]
        return list(explicit)

    env = os.environ.get("LIBHARNESS_PI_PATH")
    if env:
        return [env]

    vendored = vendored_pi_binary(package_root=package_root)
    if vendored.exists():
        return [str(vendored)]

    raise PiVendorError(
        "could not locate pi binary. Tried:\n"
        "  (1) explicit pi_command argument — not provided\n"
        "  (2) LIBHARNESS_PI_PATH env var — not set\n"
        f"  (3) vendored binary at {vendored} — not present\n"
        "To fix: run `make install-pi` to populate the vendored binary, "
        "OR set LIBHARNESS_PI_PATH=/path/to/pi, "
        "OR pass pi_command=[...] to PiLaunchConfig explicitly."
    )


_USAGE = (
    "usage: python -m libharness._pi_vendor [install [--force] | --version | --which]"
)


def _main(argv: list[str]) -> int:
    if not argv or argv[0] == "install":
        force = "--force" in argv[1:]
        try:
            path = fetch_and_extract(force=force)
        except PiVendorError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"installed pi {PI_VERSION} at {path}")
        return 0
    if argv[0] == "--version":
        print(PI_VERSION)
        return 0
    if argv[0] == "--which":
        try:
            cmd = resolve_pi_command()
        except PiVendorError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(" ".join(cmd))
        return 0
    print(f"unknown command: {argv[0]!r}", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
