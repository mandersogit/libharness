"""Small utility CLI for inspecting generated bridge artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path

from .shim import write_shim


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Utilities for pi-python-harness")
    sub = parser.add_subparsers(dest="cmd", required=True)
    shim = sub.add_parser("write-shim", help="write the TypeScript bridge extension")
    shim.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "write-shim":
        write_shim(args.path)
        print(args.path)
        return 0
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
