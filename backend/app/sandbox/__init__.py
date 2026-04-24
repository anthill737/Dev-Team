"""Sandbox module — scopes agent command execution and file I/O to the project directory.

v1 implementation uses process-level sandboxing: a command whitelist, timeouts, output caps,
path-boundary enforcement. Not true isolation (no container), but a real safety layer.

When running on a machine with Docker/WSL2/Windows Sandbox available, the same interface
can be backed by a container executor. The Coder tools depend only on the `SandboxExecutor`
protocol, so swapping backends doesn't touch the agent layer.
"""

from .executor import (
    CommandDenied,
    CommandResult,
    PathOutsideProject,
    ProcessSandboxExecutor,
    SandboxExecutor,
)
from .fs import safe_list, safe_read, safe_write

__all__ = [
    "CommandDenied",
    "CommandResult",
    "PathOutsideProject",
    "ProcessSandboxExecutor",
    "SandboxExecutor",
    "safe_list",
    "safe_read",
    "safe_write",
]
