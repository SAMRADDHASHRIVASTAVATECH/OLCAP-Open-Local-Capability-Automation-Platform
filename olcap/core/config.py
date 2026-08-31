"""
Unified configuration for the OpenLive Capability System (OLCAP).

One configuration surface shared by all three MCP servers, the Unified Core,
the Component Manager and the platform adapters.
"""
from __future__ import annotations

import os
import platform as _platform
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

APP_NAME = "olcap"
ENV_PREFIX = "OLCAP_"


def _home() -> Path:
    env = os.environ.get(f"{ENV_PREFIX}HOME")
    if env:
        return Path(env).expanduser()
    return Path(os.path.expanduser("~")) / ".olcap"


class Config:
    """Single shared configuration object (read once, cached process-wide)."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root: Path = Path(root) if root else Path(__file__).resolve().parents[1]
        self.home: Path = _home()
        self.data: Path = self.home / "data"
        self.logs: Path = self.home / "logs"
        self.artifacts: Path = self.home / "artifacts"
        self.indexes: Path = self.home / "indexes"
        self.traces: Path = self.home / "traces"
        self.components: Path = self.home / "components"
        self.reports: Path = self.home / "reports"
        for p in (self.data, self.logs, self.artifacts, self.indexes,
                  self.traces, self.components, self.reports):
            p.mkdir(parents=True, exist_ok=True)

        self.state_db: Path = self.data / "state.db"
        self.config_dir: Path = self.root / "config"

        self.platform: str = self._detect_platform()
        self.machine_id: str = _platform.node() or "unknown"

        self._yaml: Dict[str, Dict[str, Any]] = {}
        for name in ("capabilities", "components", "permissions", "routing"):
            f = self.config_dir / f"{name}.yaml"
            if f.exists():
                self._yaml[name] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            else:
                self._yaml[name] = {}

    # ------------------------------------------------------------------ #
    def _detect_platform(self) -> str:
        forced = os.environ.get(f"{ENV_PREFIX}PLATFORM")
        if forced in ("windows", "linux", "darwin"):
            return forced
        return _platform.system().lower()

    # ------------------------------------------------------------------ #
    def section(self, name: str) -> Dict[str, Any]:
        return self._yaml.get(name, {})

    def env(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return os.environ.get(f"{ENV_PREFIX}{key.upper()}", default)

    def bool_env(self, key: str, default: bool = False) -> bool:
        v = self.env(key)
        if v is None:
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    # ------------------------------------------------------------------ #
    @property
    def is_windows(self) -> bool:
        return self.platform == "windows"

    def which(self, exe: str) -> Optional[str]:
        return shutil.which(exe)

    def has(self, exe: str) -> bool:
        return self.which(exe) is not None

    # ------------------------------------------------------------------ #
    def resource_profile(self) -> Dict[str, Any]:
        """Best-effort hardware inspection (works on Windows and Linux)."""
        profile: Dict[str, Any] = {
            "platform": self.platform,
            "platform_release": _platform.release(),
            "cpu_count": os.cpu_count() or 1,
            "ram_total_gb": None,
            "vram_total_gb": None,
            "gpu": None,
        }
        try:
            import psutil  # type: ignore

            profile["ram_total_gb"] = round(psutil.virtual_memory().total / (1024 ** 3), 2)
        except Exception:
            try:
                profile["ram_total_gb"] = round(os.sysconf("SC_PAGE_SIZE") *
                                                os.sysconf("SC_PHYS_PAGES") / (1024 ** 3), 2)
            except Exception:
                pass
        if self.is_windows:
            try:
                import subprocess

                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_VideoController | "
                     "Select-Object -ExpandProperty AdapterRAM"],
                    capture_output=True, text=True, timeout=25)
                vals = [int(x) for x in out.stdout.split() if x.isdigit()]
                if vals:
                    profile["vram_total_gb"] = round(max(vals) / (1024 ** 3), 2)
            except Exception:
                pass
        return profile


_CFG: Optional[Config] = None


def cfg(root: Optional[Path] = None) -> Config:
    global _CFG
    if _CFG is None:
        _CFG = Config(root)
    return _CFG


def reload() -> Config:
    global _CFG
    _CFG = None
    return cfg()
