"""Python-first integration scaffolding for the Pi agent harness.

This package deliberately keeps Pi's TypeScript surface small:

* :class:`PiRpcClient` launches and controls ``pi --mode rpc``.
* :class:`ToolRegistry` lets Python code define LLM-callable tools.
* :class:`PythonToolServer` exposes those tools to a generated TypeScript shim.
* :class:`PythonPiHarness` wires the pieces together.
"""

from .bridge import PythonToolServer
from .client import PiRpcClient, RpcError, RpcProcessError
from .harness import PythonPiHarness
from .tools import ToolContext, ToolRegistry, ToolResult, ToolUpdate

__all__ = [
    "PiRpcClient",
    "PythonPiHarness",
    "PythonToolServer",
    "RpcError",
    "RpcProcessError",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolUpdate",
]
