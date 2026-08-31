"""Adapter selection. Shared core logic never imports a platform module directly."""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..core.config import cfg
from .base import OSAdapter


def get_adapter(platform: Optional[str] = None) -> OSAdapter:
    p = (platform or cfg().platform).lower()
    if p == "windows":
        from .windows import WindowsAdapter
        return WindowsAdapter()
    if p in ("linux", "darwin"):
        from .linux import LinuxAdapter
        return LinuxAdapter()
    raise NotImplementedError(f"no OS adapter for platform '{p}'")


def platform_capabilities(platform: Optional[str] = None) -> Dict[str, Any]:
    try:
        return get_adapter(platform).capabilities()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
