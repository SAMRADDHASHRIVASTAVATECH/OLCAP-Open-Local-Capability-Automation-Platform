"""Linux OS adapter - same contract as the Windows adapter."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import OSAdapter, SharedLogic

try:
    import psutil  # type: ignore
except Exception:
    psutil = None


class LinuxAdapter(OSAdapter):
    name = "linux"

    # ------------------------------------------------------------------ info --
    def info(self) -> Dict[str, Any]:
        return {"platform": "linux", "release": platform.release(),
                "machine": platform.machine(), "display": os.environ.get("DISPLAY", ""),
                "wayland": os.environ.get("WAYLAND_DISPLAY", ""),
                "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
                "psutil": psutil is not None}

    def capabilities(self) -> Dict[str, bool]:
        has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        return {
            "filesystem": True, "terminal": True, "processes": psutil is not None,
            "os_control": True, "gui": has_display,
            "screenshot": has_display and (shutil.which("import") is not None
                                           or shutil.which("gnome-screenshot") is not None
                                           or shutil.which("scrot") is not None
                                           or self._pillow_ok()),
            "window_management": has_display and (shutil.which("xdotool") is not None
                                                  or shutil.which("wmctrl") is not None),
        }

    @staticmethod
    def _pillow_ok() -> bool:
        try:
            import PIL  # noqa: F401
            return True
        except Exception:
            return False

    # ------------------------------------------------------------- filesystem --
    def fs_list(self, path: str, pattern: str = "*", recursive: bool = False,
                limit: int = 500) -> List[Dict[str, Any]]:
        p = Path(path).expanduser()
        pattern = (pattern or '*').strip() or '*'
        it = p.rglob(pattern) if recursive else p.glob(pattern)
        out = []
        for f in it:
            try:
                st = f.stat()
                out.append({"path": str(f), "name": f.name, "is_dir": f.is_dir(),
                            "size": st.st_size, "modified": st.st_mtime})
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out

    def fs_read(self, path: str, encoding: str = "utf-8", limit: int = 200000) -> str:
        return SharedLogic.read_text(path, encoding, limit)

    def fs_write(self, path: str, content: str, append: bool = False,
                 encoding: str = "utf-8") -> Dict[str, Any]:
        return SharedLogic.write_text(path, content, append, encoding)

    def fs_delete(self, path: str) -> Dict[str, Any]:
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return {"ok": True, "path": str(p)}

    def fs_move(self, path: str, destination: str) -> Dict[str, Any]:
        shutil.move(path, destination)
        return {"ok": True, "from": path, "to": destination}

    def fs_copy(self, path: str, destination: str) -> Dict[str, Any]:
        p = Path(path)
        if p.is_dir():
            shutil.copytree(p, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(p, destination)
        return {"ok": True, "from": path, "to": destination}

    def fs_stat(self, path: str) -> Dict[str, Any]:
        return SharedLogic.stat(path)

    # --------------------------------------------------------------- terminal --
    def terminal(self, command, cwd: Optional[str] = None, timeout_s: int = 120,
                 env: Optional[Dict[str, str]] = None,
                 shell: Optional[object] = None) -> Dict[str, Any]:
        """`command` is either an argv list (no shell) or a shell string.

        argv mode is the default: shell metacharacters in untrusted text
        cannot spawn extra commands that way.
        """
        if isinstance(command, (list, tuple)):
            return SharedLogic.run(list(command), cwd=cwd, timeout_s=timeout_s,
                                   env=env, shell_cmd=None)
        if shell:
            return SharedLogic.run([], cwd=cwd, timeout_s=timeout_s, env=env,
                                   shell_cmd=str(command))
        # No shell requested but a string was passed: split it defensively.
        import shlex as _shlex
        argv = _shlex.split(str(command), posix=not str(getattr(self, "name", "") or "").lower().startswith("win"))
        return SharedLogic.run(argv, cwd=cwd, timeout_s=timeout_s, env=env,
                               shell_cmd=None)

    # -------------------------------------------------------------- processes --
    def process_list(self, name: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if psutil:
            for pr in psutil.process_iter(["pid", "name", "exe", "cmdline",
                                           "memory_info", "create_time"]):
                try:
                    i = pr.info
                    if name and name.lower() not in (i.get("name") or "").lower():
                        continue
                    out.append({"pid": i["pid"], "name": i.get("name"),
                                "exe": i.get("exe"),
                                "memory_mb": round((i.get("memory_info").rss /
                                                    1048576) if i.get("memory_info") else 0, 1),
                                "cmdline": " ".join(i.get("cmdline") or [])[:200]})
                    if len(out) >= limit:
                        break
                except Exception:
                    continue
        else:
            r = SharedLogic.run([], shell_cmd="ps -eo pid,comm,args --no-headers")
            for line in r.get("stdout", "").splitlines()[:limit]:
                parts = line.split(None, 2)
                if len(parts) >= 2:
                    out.append({"pid": int(parts[0]), "name": parts[1],
                                "cmdline": parts[2][:200] if len(parts) > 2 else ""})
        return out

    def process_start(self, argv: List[str], cwd: Optional[str] = None,
                      env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        e = {**os.environ, **(env or {})}
        p = subprocess.Popen(argv, cwd=cwd, env=e,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "pid": p.pid, "argv": argv}

    def process_kill(self, pid: int, force: bool = True) -> Dict[str, Any]:
        if psutil:
            pr = psutil.Process(pid)
            pr.kill() if force else pr.terminate()
        else:
            SharedLogic.run([], shell_cmd=f"kill {'-9' if force else '-TERM'} {pid}")
        return {"ok": True, "pid": pid, "force": force}

    # --------------------------------------------------------------- os misc --
    def os_control(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if action == "env_get":
            key = params.get("key") or ""
            return {"ok": True, "key": key, "value": os.environ.get(key)}
        if action == "env_list":
            return {"ok": True, "count": len(os.environ),
                    "env": {k: v for k, v in list(os.environ.items())[:200]}}
        if action == "services":
            r = SharedLogic.run([], shell_cmd="systemctl list-units --type=service "
                                              "--no-pager --no-legend || true",
                                timeout_s=30)
            svcs = []
            for line in r.get("stdout", "").splitlines():
                parts = line.split(None, 4)
                if len(parts) >= 4:
                    svcs.append({"unit": parts[0], "load": parts[1],
                                 "active": parts[2], "sub": parts[3]})
            return {"ok": True, "services": svcs[:100]}
        if action == "info":
            return {"ok": True, **self.info()}
        if action == "capabilities":
            return {"ok": True, **self.capabilities()}
        if action == "which":
            return {"ok": True, "path": shutil.which(params.get("name", ""))}
        if action == "notify":
            r = SharedLogic.run([], shell_cmd=f"notify-send '{params.get('title','olcap')}' "
                                              f"'{params.get('message','')}' || true")
            return {"ok": True, "delivered": r.get("exit_code") == 0}
        return {"ok": False, "error": f"unsupported os action on linux: {action}"}

    # -------------------------------------------------------------------- gui --
    def gui_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.capabilities()["gui"]:
            return {"ok": False, "degraded": True,
                    "error": "no X/Wayland display available (headless environment)"}
        if action == "click":
            self._xdotool(["mousemove", str(params.get("x", 0)), str(params.get("y", 0)),
                           "click", str(params.get("button", 1))])
            return {"ok": True}
        if action == "type":
            self._xdotool(["type", "--delay", "12", params.get("text", "")])
            return {"ok": True}
        if action == "key":
            self._xdotool(["key", params.get("combo", "")])
            return {"ok": True}
        if action == "move":
            self._xdotool(["mousemove", str(params.get("x", 0)), str(params.get("y", 0))])
            return {"ok": True}
        return {"ok": False, "error": f"unsupported gui action: {action}"}

    def _xdotool(self, args: List[str]) -> Dict[str, Any]:
        if not shutil.which("xdotool"):
            return {"ok": False, "error": "xdotool not installed"}
        return SharedLogic.run(["xdotool", *args], timeout_s=30)

    # ------------------------------------------------------------- screenshot --
    def screenshot(self, target: str = "screen", region: Optional[List[int]] = None,
                   path: str = "") -> Dict[str, Any]:
        if not self.capabilities()["screenshot"]:
            return {"ok": False, "degraded": True,
                    "error": "no screenshot backend available (headless/no X display)"}
        out = path or str(Path(os.path.expanduser("~")) /
                          f"olcap_shot_{int(__import__('time').time())}.png")
        if shutil.which("import"):
            r = SharedLogic.run(["import", "-window", "root", out], timeout_s=40)
        elif shutil.which("gnome-screenshot"):
            r = SharedLogic.run(["gnome-screenshot", "-f", out], timeout_s=40)
        elif shutil.which("scrot"):
            r = SharedLogic.run(["scrot", out], timeout_s=40)
        else:
            try:
                from PIL import ImageGrab  # type: ignore
                ImageGrab.grab().save(out)
                r = {"ok": True}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": r.get("ok", False), "path": out, "target": target,
                "exists": Path(out).exists()}

    # ---------------------------------------------------------------- windows --
    def window_manage(self, action: str, title: str = "",
                      params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        if not self.capabilities()["window_management"]:
            return {"ok": False, "degraded": True,
                    "error": "xdotool/wmctrl unavailable or no display"}
        if action == "list":
            r = SharedLogic.run(["wmctrl", "-l"], timeout_s=20)
            wins = []
            for line in r.get("stdout", "").splitlines():
                parts = line.split(None, 3)
                if len(parts) >= 4:
                    wins.append({"id": parts[0], "desktop": parts[1],
                                 "host": parts[2], "title": parts[3]})
            return {"ok": True, "windows": wins}
        if action == "focus":
            return self._xdotool(["search", "--name", title, "windowactivate"])
        if action == "close":
            return self._xdotool(["search", "--name", title, "windowkill"])
        return {"ok": False, "error": f"unsupported window action: {action}"}
