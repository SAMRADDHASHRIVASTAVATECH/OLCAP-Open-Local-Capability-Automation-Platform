"""
Platform abstraction.

    SHARED LOGIC -> PLATFORM ABSTRACTION -> OS ADAPTER -> NATIVE IMPLEMENTATION

Shared capability contracts live here. Nothing OS-specific may leak into the
shared core; every native detail is behind an adapter method. Sensitive
operations are gated by the permission policy in core/permissions.py, never by
the adapter itself.
"""
from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional


class OSAdapter(ABC):
    """Identical contract on Windows and Linux."""

    name = "base"

    # ---------------------------------------------------------------- info --
    @abstractmethod
    def info(self) -> Dict[str, Any]:
        ...

    @abstractmethod
    def capabilities(self) -> Dict[str, bool]:
        """Which native operations this platform actually supports right now."""

    # ------------------------------------------------------------ filesystem --
    @abstractmethod
    def fs_list(self, path: str, pattern: str = "*", recursive: bool = False,
                limit: int = 500) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def fs_read(self, path: str, encoding: str = "utf-8", limit: int = 200000) -> str:
        ...

    @abstractmethod
    def fs_write(self, path: str, content: str, append: bool = False,
                 encoding: str = "utf-8") -> Dict[str, Any]:
        ...

    @abstractmethod
    def fs_delete(self, path: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def fs_move(self, path: str, destination: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def fs_copy(self, path: str, destination: str) -> Dict[str, Any]:
        ...

    @abstractmethod
    def fs_stat(self, path: str) -> Dict[str, Any]:
        ...

    # -------------------------------------------------------------- terminal --
    @abstractmethod
    def terminal(self, command: str, cwd: Optional[str] = None,
                 timeout_s: int = 120, env: Optional[Dict[str, str]] = None,
                 shell: Optional[str] = None) -> Dict[str, Any]:
        ...

    # -------------------------------------------------------------- processes --
    @abstractmethod
    def process_list(self, name: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def process_start(self, argv: List[str], cwd: Optional[str] = None,
                      env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        ...

    @abstractmethod
    def process_kill(self, pid: int, force: bool = True) -> Dict[str, Any]:
        ...

    # --------------------------------------------------------- os-level misc --
    @abstractmethod
    def os_control(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        ...

    # ------------------------------------------------------------------- gui --
    @abstractmethod
    def gui_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        ...

    @abstractmethod
    def screenshot(self, target: str = "screen", region: Optional[List[int]] = None,
                   path: str = "") -> Dict[str, Any]:
        ...

    @abstractmethod
    def window_manage(self, action: str, title: str = "",
                      params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ...


class SharedLogic:
    """Platform-independent helpers reused by every adapter."""

    @staticmethod
    def read_text(path: str, encoding: str = "utf-8", limit: int = 200000) -> str:
        p = Path(path)
        data = p.read_bytes()
        try:
            return data[:limit].decode(encoding, errors="replace")
        except LookupError:
            return data[:limit].decode("utf-8", errors="replace")

    @staticmethod
    def write_text(path: str, content: str, append: bool = False,
                   encoding: str = "utf-8") -> Dict[str, Any]:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with p.open(mode, encoding=encoding, errors="replace") as fh:
            fh.write(content)
        return {"ok": True, "path": str(p), "bytes": p.stat().st_size}

    @staticmethod
    def run(argv: List[str], cwd: Optional[str] = None, timeout_s: int = 120,
            env: Optional[Dict[str, str]] = None, shell_cmd: Optional[str] = None
            ) -> Dict[str, Any]:
        clean_env = None
        if env:
            clean_env = {**os.environ, **env}
        cwd = cwd or None            # "" would make subprocess chdir("") and fail
        cmd = shell_cmd if shell_cmd else argv
        use_shell = bool(shell_cmd)
        try:
            r = subprocess.run(cmd, cwd=cwd, env=clean_env, timeout=timeout_s,
                               capture_output=True, text=True, shell=use_shell,
                               errors="replace")
            return {"exit_code": r.returncode, "stdout": r.stdout[-50000:],
                    "stderr": r.stderr[-20000:], "ok": r.returncode == 0}
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "stdout": "", "stderr": f"timeout after {timeout_s}s",
                    "ok": False, "timeout": True}
        except FileNotFoundError as e:
            return {"exit_code": -2, "stdout": "", "stderr": str(e), "ok": False}
        except Exception as e:
            return {"exit_code": -3, "stdout": "", "stderr": f"{type(e).__name__}: {e}",
                    "ok": False}

    @staticmethod
    def stat(path: str) -> Dict[str, Any]:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(path)
        st = p.stat()
        return {"path": str(p), "is_dir": p.is_dir(), "size": st.st_size,
                "modified": st.st_mtime, "created": getattr(st, "st_ctime", st.st_mtime)}
