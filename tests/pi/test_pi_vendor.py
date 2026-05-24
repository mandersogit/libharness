"""Unit tests for libharness._pi_vendor.

Hermetic — no network. Network-touching paths (fetch_and_extract) are
exercised by the integration tests + the make install-pi loop.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from libharness import _pi_vendor as pv


class TestDetectPlatform:
    def test_linux_arm64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pv.platform, "system", lambda: "Linux")
        monkeypatch.setattr(pv.platform, "machine", lambda: "aarch64")
        assert pv.detect_platform() == "linux-arm64"

    def test_linux_x64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pv.platform, "system", lambda: "Linux")
        monkeypatch.setattr(pv.platform, "machine", lambda: "x86_64")
        assert pv.detect_platform() == "linux-x64"

    def test_darwin_arm64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pv.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(pv.platform, "machine", lambda: "arm64")
        assert pv.detect_platform() == "darwin-arm64"

    def test_darwin_x64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pv.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(pv.platform, "machine", lambda: "x86_64")
        assert pv.detect_platform() == "darwin-x64"

    def test_windows_x64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pv.platform, "system", lambda: "Windows")
        monkeypatch.setattr(pv.platform, "machine", lambda: "AMD64")
        assert pv.detect_platform() == "windows-x64"

    def test_windows_arm64(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pv.platform, "system", lambda: "Windows")
        monkeypatch.setattr(pv.platform, "machine", lambda: "ARM64")
        assert pv.detect_platform() == "windows-arm64"

    def test_unsupported_os(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pv.platform, "system", lambda: "FreeBSD")
        monkeypatch.setattr(pv.platform, "machine", lambda: "x86_64")
        with pytest.raises(pv.PiVendorError, match="unsupported OS"):
            pv.detect_platform()

    def test_unsupported_arch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pv.platform, "system", lambda: "Linux")
        monkeypatch.setattr(pv.platform, "machine", lambda: "riscv64")
        with pytest.raises(pv.PiVendorError, match="unsupported architecture"):
            pv.detect_platform()


class TestPlatformHashes:
    """The hash table is the source of truth for what we'll fetch."""

    def test_all_six_platforms_present(self) -> None:
        expected = {
            "darwin-arm64", "darwin-x64",
            "linux-arm64", "linux-x64",
            "windows-arm64", "windows-x64",
        }
        assert set(pv._PLATFORM_HASHES.keys()) == expected

    def test_hashes_look_like_sha256(self) -> None:
        for plat, (name, sha) in pv._PLATFORM_HASHES.items():
            assert len(sha) == 64, f"{plat}: sha256 should be 64 hex chars, got {len(sha)}"
            int(sha, 16)  # raises if not hex
            assert name.startswith(f"pi-{plat}."), f"{plat}: asset name mismatch: {name}"


class TestVendoredPathHelpers:
    def test_vendored_pi_dir_under_package(self, tmp_path: Path) -> None:
        result = pv.vendored_pi_dir(package_root=tmp_path)
        assert result == tmp_path / "_vendor" / "pi"

    def test_vendored_pi_binary_unix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pv.sys, "platform", "linux")
        result = pv.vendored_pi_binary(package_root=tmp_path)
        assert result == tmp_path / "_vendor" / "pi" / "pi"

    def test_vendored_pi_binary_windows(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pv.sys, "platform", "win32")
        result = pv.vendored_pi_binary(package_root=tmp_path)
        assert result == tmp_path / "_vendor" / "pi" / "pi.exe"

    def test_default_package_root_is_module_dir(self) -> None:
        # When package_root is None, vendored_pi_dir should resolve relative
        # to the _pi_vendor.py module file (i.e. src/libharness/_vendor/pi/
        # in dev, site-packages/libharness/_vendor/pi/ in prod).
        result = pv.vendored_pi_dir()
        expected_parent = Path(pv.__file__).resolve().parent
        assert result == expected_parent / "_vendor" / "pi"


class TestResolvePiCommand:
    """Resolver precedence: explicit > LIBHARNESS_PI_PATH > vendored > fail loud."""

    def test_explicit_string_wins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIBHARNESS_PI_PATH", "/should/be/ignored")
        result = pv.resolve_pi_command(explicit="/my/pi", package_root=tmp_path)
        assert result == ["/my/pi"]

    def test_explicit_sequence_wins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIBHARNESS_PI_PATH", "/should/be/ignored")
        result = pv.resolve_pi_command(
            explicit=["node", "/path/to/cli.js"],
            package_root=tmp_path,
        )
        assert result == ["node", "/path/to/cli.js"]

    def test_env_var_used_when_no_explicit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LIBHARNESS_PI_PATH", "/from/env/pi")
        result = pv.resolve_pi_command(package_root=tmp_path)
        assert result == ["/from/env/pi"]

    def test_vendored_used_when_no_explicit_no_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("LIBHARNESS_PI_PATH", raising=False)
        vendored = pv.vendored_pi_binary(package_root=tmp_path)
        vendored.parent.mkdir(parents=True)
        vendored.write_text("#!/bin/sh\necho fake-pi\n")
        vendored.chmod(0o755)
        result = pv.resolve_pi_command(package_root=tmp_path)
        assert result == [str(vendored)]

    def test_fail_loud_when_nothing_found(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("LIBHARNESS_PI_PATH", raising=False)
        with pytest.raises(pv.PiVendorError) as excinfo:
            pv.resolve_pi_command(package_root=tmp_path)
        msg = str(excinfo.value)
        # The diagnostic must mention all three checked paths so users can act.
        assert "explicit pi_command" in msg
        assert "LIBHARNESS_PI_PATH" in msg
        assert "vendored binary" in msg
        # And the remediation hints must be actionable.
        assert "make install-pi" in msg
        assert "LIBHARNESS_PI_PATH" in msg
        assert "PiLaunchConfig" in msg

    def test_no_path_fallback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Resolver must NOT consult $PATH even if a `pi` binary is on it."""
        monkeypatch.delenv("LIBHARNESS_PI_PATH", raising=False)
        # Create a fake pi on PATH; the resolver should ignore it.
        fake_path_dir = tmp_path / "fake_path"
        fake_path_dir.mkdir()
        fake_pi = fake_path_dir / "pi"
        fake_pi.write_text("#!/bin/sh\n")
        fake_pi.chmod(0o755)
        monkeypatch.setenv("PATH", str(fake_path_dir))
        # Use a package_root that doesn't have a vendored binary either.
        empty_root = tmp_path / "empty_pkg"
        empty_root.mkdir()
        with pytest.raises(pv.PiVendorError):
            pv.resolve_pi_command(package_root=empty_root)


class TestPiLaunchConfigDefersToResolver:
    """PiLaunchConfig.base_argv() should call the resolver when pi_command is None."""

    def test_none_default_invokes_resolver(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from libharness.pi.rpc import PiLaunchConfig

        # No explicit command, no env, no vendored — should raise.
        monkeypatch.delenv("LIBHARNESS_PI_PATH", raising=False)
        # Make sure the resolver doesn't accidentally find a real vendored binary
        # by pointing it at an empty package root via the env override.
        with mock.patch.object(
            pv, "vendored_pi_binary", return_value=Path("/nonexistent/pi")
        ):
            config = PiLaunchConfig()
            with pytest.raises(pv.PiVendorError):
                config.base_argv()

    def test_explicit_string_pi_command(self) -> None:
        from libharness.pi.rpc import PiLaunchConfig

        config = PiLaunchConfig(pi_command="/explicit/pi")
        argv = config.base_argv()
        assert argv[0] == "/explicit/pi"
        assert "--mode" in argv and "rpc" in argv

    def test_explicit_sequence_pi_command(self) -> None:
        from libharness.pi.rpc import PiLaunchConfig

        config = PiLaunchConfig(pi_command=["node", "/path/to/cli.js"])
        argv = config.base_argv()
        assert argv[:2] == ["node", "/path/to/cli.js"]
        assert "--mode" in argv and "rpc" in argv

    def test_env_override_via_resolver(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from libharness.pi.rpc import PiLaunchConfig

        monkeypatch.setenv("LIBHARNESS_PI_PATH", "/from/env/pi")
        config = PiLaunchConfig()
        argv = config.base_argv()
        assert argv[0] == "/from/env/pi"


class TestFetchSha256Mismatch:
    """fetch_and_extract should raise on hash mismatch, not extract garbage."""

    def test_sha256_mismatch_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Stub the downloader to write garbage that will fail sha256 verification.
        def fake_download(url: str, dest: Path) -> None:
            dest.write_bytes(b"not the real pi tarball")

        monkeypatch.setattr(pv, "_download_to", fake_download)
        # Force the resolver to think we want linux-arm64 regardless of host.
        monkeypatch.setattr(pv, "detect_platform", lambda: "linux-arm64")

        with pytest.raises(pv.PiVendorError, match="sha256 mismatch"):
            pv.fetch_and_extract(package_root=tmp_path, force=True)

    def test_idempotent_when_binary_exists(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pre-create the target binary.
        target = pv.vendored_pi_binary(package_root=tmp_path)
        target.parent.mkdir(parents=True)
        target.write_text("pretend pi")

        # Make download fail; fetch_and_extract should skip it.
        def fake_download(url: str, dest: Path) -> None:
            raise AssertionError("should not have attempted download")

        monkeypatch.setattr(pv, "_download_to", fake_download)
        result = pv.fetch_and_extract(package_root=tmp_path)
        assert result == target
