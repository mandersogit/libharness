"""Scaffold-only smoke test. Replace with real tests as code lands."""

import libharness


def test_package_importable() -> None:
    assert libharness.__version__ == "0.0.1"
