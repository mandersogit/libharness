from __future__ import annotations

import argparse
import importlib
import sys
from typing import Sequence

from .tools import default_registry, serve_jsonl


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Pi Python tool JSONL server")
    parser.add_argument("module", nargs="?", help="Python module to import before serving tools")
    args = parser.parse_args(argv)
    if args.module:
        importlib.import_module(args.module)
    serve_jsonl(default_registry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
