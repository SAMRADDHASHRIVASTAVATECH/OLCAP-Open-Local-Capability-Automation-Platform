"""
Capability implementations.

Importing this package binds every @implements() decorator into the runtime
registry. It is imported lazily (from Runtime.__init__) because the modules
here import `runtime` for the decorator - importing them at runtime-module
import time would be circular.
"""
from __future__ import annotations

_IMPL_MODULES = ("web", "research", "knowledge", "dataops", "automation", "os_ops")


def register_all() -> None:
    import importlib
    for name in _IMPL_MODULES:
        importlib.import_module(f"{__name__}.{name}")
