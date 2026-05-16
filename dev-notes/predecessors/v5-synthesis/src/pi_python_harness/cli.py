from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .shim import write_bridge_shim


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Utilities for pi-python-harness")
    sub = parser.add_subparsers(dest="command", required=True)
    shim = sub.add_parser("write-shim", help="write the generic TypeScript bridge extension")
    shim.add_argument("path", type=Path)
    shim.add_argument("--no-diagnostic-commands", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "write-shim":
        path = write_bridge_shim(args.path, diagnostic_commands=not args.no_diagnostic_commands)
        print(path)
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
