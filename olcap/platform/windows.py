"""
Windows OS adapter.

SHARED LOGIC -> PLATFORM ABSTRACTION -> WINDOWS OS ADAPTER -> NATIVE IMPLEMENTATION

Native calls use ctypes against user32/kernel32/advapi32 (stdlib only), with
PowerShell fallbacks. Nothing here is imported on Linux, so Windows specifics
can never leak into shared core logic.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import OSAdapter, SharedLogic

try:
    import psutil  # type: ignore
except Exception:
    psutil = None

user32 = ctypes.windll.user32  # type: ignore[attr-defined]
kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]


def _ps(script: str, timeout: int = 60) -> Dict[str, Any]:
    return SharedLogic.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout_s=timeout)


class WindowsAdapter(OSAdapter):
    name = "windows"

    # ------------------------------------------------------------------ info --
    def info(self) -> Dict[str, Any]:
        import platform
        r = _ps("[System.Environment]::OSVersion.VersionString; "
                "$os=Get-CimInstance Win32_OperatingSystem; $os.Caption; "
                "$os.TotalVisibleMemorySize; "
                "$cs=Get-CimInstance Win32_ComputerSystem; $cs.TotalPhysicalMemory",
                timeout=45)
        lines = (r.get("stdout") or "").strip().splitlines()
        return {"platform": "windows", "release": platform.release(),
                "version": lines[0] if lines else "",
                "caption": lines[1] if len(lines) > 1 else "",
                "psutil": psutil is not None,
                "has_display": bool(user32.GetDesktopWindow())}

    def capabilities(self) -> Dict[str, bool]:
        try:
            import PIL  # noqa: F401
            pillow = True
        except Exception:
            pillow = False
        return {"filesystem": True, "terminal": True, "processes": True,
                "os_control": True, "gui": True,
                "screenshot": pillow or shutil.which("powershell") is not None,
                "window_management": True}

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
        shutil.move(path, destination); return {"ok": True, "from": path, "to": destination}

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
        """`command` is either an argv list (no shell) or a command string.

        argv mode is the default. On Windows a string without shell=True is
        still executed through cmd.exe (backslash path handling), but the
        caller in os_ops has already split it, so metacharacters arriving
        from untrusted text are inert arguments rather than new commands.
        """
        if isinstance(command, (list, tuple)):
            return SharedLogic.run(list(command), cwd=cwd, timeout_s=timeout_s,
                                   env=env)
        if shell and str(shell).lower().startswith("pwsh"):
            argv = ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                    str(command)]
            return SharedLogic.run(argv, cwd=cwd, timeout_s=timeout_s, env=env)
        return SharedLogic.run([], cwd=cwd, timeout_s=timeout_s, env=env,
                               shell_cmd=str(command))

    # -------------------------------------------------------------- processes --
    def process_list(self, name: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        if psutil:
            out = []
            for pr in psutil.process_iter(["pid", "name", "exe", "cmdline", "memory_info"]):
                try:
                    i = pr.info
                    if name and name.lower() not in (i.get("name") or "").lower():
                        continue
                    out.append({"pid": i["pid"], "name": i.get("name"),
                                "exe": i.get("exe"),
                                "memory_mb": round((i["memory_info"].rss / 1048576)
                                                   if i.get("memory_info") else 0, 1),
                                "cmdline": " ".join(i.get("cmdline") or [])[:200]})
                    if len(out) >= limit:
                        break
                except Exception:
                    continue
            return out
        r = _ps("Get-Process | Select-Object Id,ProcessName,WorkingSet64 | "
                "ConvertTo-Json -Depth 2", timeout=45)
        import json
        try:
            data = json.loads(r.get("stdout") or "[]")
            if isinstance(data, dict):
                data = [data]
            return [{"pid": d["Id"], "name": d["ProcessName"],
                     "memory_mb": round(d.get("WorkingSet64", 0) / 1048576, 1)}
                    for d in data[:limit]]
        except Exception:
            return []

    def process_start(self, argv: List[str], cwd: Optional[str] = None,
                      env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        e = {**os.environ, **(env or {})}
        p = subprocess.Popen(argv, cwd=cwd, env=e,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return {"ok": True, "pid": p.pid, "argv": argv}

    def process_kill(self, pid: int, force: bool = True) -> Dict[str, Any]:
        if psutil:
            pr = psutil.Process(pid)
            pr.kill() if force else pr.terminate()
        else:
            _ps(f"Stop-Process -Id {pid} -Force")
        return {"ok": True, "pid": pid, "force": force}

    # --------------------------------------------------------------- os misc --
    def os_control(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if action == "env_get":
            key = params.get("key") or ""
            scope = params.get("scope", "process")
            if scope == "process":
                return {"ok": True, "key": key, "value": os.environ.get(key)}
            r = _ps(f"[Environment]::GetEnvironmentVariable('{key}','{scope.capitalize()}')")
            return {"ok": True, "key": key, "value": (r.get("stdout") or "").strip(),
                    "scope": scope}
        if action == "env_set":
            key, val = params.get("key"), params.get("value", "")
            scope = params.get("scope", "user")
            r = _ps(f"[Environment]::SetEnvironmentVariable('{key}','{val}','"
                    f"{scope.capitalize()}')")
            return {"ok": r.get("exit_code") == 0, "key": key, "scope": scope}
        if action == "env_list":
            return {"ok": True, "count": len(os.environ),
                    "env": dict(list(os.environ.items())[:200])}
        if action == "services":
            r = _ps("Get-Service | Select-Object Name,Status,DisplayName | "
                    "ConvertTo-Json -Depth 2", timeout=60)
            import json
            try:
                data = json.loads(r.get("stdout") or "[]")
                if isinstance(data, dict):
                    data = [data]
                return {"ok": True, "services": [{"name": d["Name"],
                                                  "status": str(d.get("Status")),
                                                  "display": d.get("DisplayName")}
                                                 for d in data]}
            except Exception:
                return {"ok": False, "error": "could not parse service list"}
        if action == "service_control":
            name, op = params.get("name"), params.get("operation", "status")
            r = _ps(f"{op.capitalize()}-Service -Name '{name}' -Force -ErrorAction Stop; "
                    f"(Get-Service -Name '{name}').Status", timeout=90)
            return {"ok": r.get("exit_code") == 0,
                    "status": (r.get("stdout") or "").strip()}
        if action == "registry_get":
            r = _ps(f"(Get-ItemProperty -Path '{params.get('path')}' "
                    f"-Name '{params.get('name')}' -ErrorAction Stop)."
                    f"'{params.get('name')}'")
            return {"ok": r.get("exit_code") == 0, "value": (r.get("stdout") or "").strip()}
        if action == "clipboard_get":
            r = _ps("Get-Clipboard -Raw")
            return {"ok": True, "text": r.get("stdout") or ""}
        if action == "clipboard_set":
            text = (params.get("text") or "").replace("'", "''")
            r = _ps(f"Set-Clipboard -Value '{text}'")
            return {"ok": r.get("exit_code") == 0}
        if action == "notify":
            title = (params.get("title") or "olcap").replace("'", "''")
            msg = (params.get("message") or "").replace("'", "''")
            _ps("Add-Type -AssemblyName System.Windows.Forms; "
                f"$n=New-Object System.Windows.Forms.NotifyIcon; "
                f"$n.Icon=[System.Drawing.SystemIcons]::Information; "
                f"$n.BalloonTipTitle='{title}'; $n.BalloonTipText='{msg}'; "
                f"$n.Visible=$true; $n.ShowBalloonTip(5000); Start-Sleep -Seconds 2; "
                f"$n.Dispose()", timeout=45)
            return {"ok": True, "delivered": True}
        if action == "info":
            return {"ok": True, **self.info()}
        if action == "capabilities":
            return {"ok": True, **self.capabilities()}
        if action == "which":
            return {"ok": True, "path": shutil.which(params.get("name", ""))}
        if action == "open":
            os.startfile(params.get("path") or params.get("url"))  # type: ignore[attr-defined]
            return {"ok": True}
        return {"ok": False, "error": f"unsupported os action: {action}"}

    # -------------------------------------------------------------------- gui --
    def gui_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if action == "click":
            user32.SetCursorPos(int(params.get("x", 0)), int(params.get("y", 0)))
            _mouse_event(0x0002 if params.get("button", 1) == 1 else 0x0008)
            _mouse_event(0x0004 if params.get("button", 1) == 1 else 0x0010)
            return {"ok": True}
        if action == "move":
            user32.SetCursorPos(int(params.get("x", 0)), int(params.get("y", 0)))
            return {"ok": True}
        if action == "scroll":
            _mouse_event(0x0800, int(params.get("delta", -120)))
            return {"ok": True}
        if action == "key":
            return _send_keys(str(params.get("combo", "")))
        if action == "type":
            return _type_text(str(params.get("text", "")))
        return {"ok": False, "error": f"unsupported gui action: {action}"}

    # ------------------------------------------------------------- screenshot --
    def screenshot(self, target: str = "screen", region: Optional[List[int]] = None,
                   path: str = "") -> Dict[str, Any]:
        out = Path(path) if path else Path(os.path.expanduser("~")) / \
            f"olcap_shot_{int(time.time())}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import ImageGrab  # type: ignore
            img = ImageGrab.grab(bbox=tuple(region) if region else None)
            img.save(str(out))
            return {"ok": True, "path": str(out), "target": target,
                    "size": list(img.size)}
        except Exception as e:
            # PowerShell fallback (no Pillow required)
            ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                  "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                  "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
                  "$g=[System.Drawing.Graphics]::FromImage($bmp); "
                  "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size); "
                  f"$bmp.Save('{str(out)}'); $g.Dispose(); $bmp.Dispose()")
            r = _ps(ps, timeout=60)
            return {"ok": out.exists(), "path": str(out), "target": target,
                    "method": "powershell", "error": None if out.exists()
                    else f"{type(e).__name__}: {e}"}

    # ---------------------------------------------------------------- windows --
    def window_manage(self, action: str, title: str = "",
                      params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        if action == "list":
            wins: List[Dict[str, Any]] = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            def cb(hwnd, _):
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    rect = wt.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    if buf.value:
                        wins.append({"hwnd": int(hwnd), "title": buf.value,
                                     "rect": [rect.left, rect.top,
                                              rect.right - rect.left,
                                              rect.bottom - rect.top]})
                return True
            user32.EnumWindows(cb, 0)
            return {"ok": True, "windows": wins[:200]}
        hwnd = _find_window(title)
        if not hwnd:
            return {"ok": False, "error": f"window not found: {title}"}
        if action == "focus":
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            return {"ok": True, "hwnd": hwnd}
        if action in ("move", "resize"):
            x, y = int(params.get("x", 0)), int(params.get("y", 0))
            w = int(params.get("width", 0)) or 800
            h = int(params.get("height", 0)) or 600
            user32.MoveWindow(hwnd, x, y, w, h, True)
            return {"ok": True, "hwnd": hwnd}
        if action == "minimize":
            user32.ShowWindow(hwnd, 6); return {"ok": True, "hwnd": hwnd}
        if action == "maximize":
            user32.ShowWindow(hwnd, 3); return {"ok": True, "hwnd": hwnd}
        if action == "close":
            user32.PostMessageW(hwnd, 0x0010, 0, 0); return {"ok": True, "hwnd": hwnd}
        return {"ok": False, "error": f"unsupported window action: {action}"}


# --------------------------------------------------------------------------- #
def _mouse_event(flags: int, data: int = 0) -> None:
    user32.mouse_event(flags, 0, 0, data, 0)


def _find_window(title: str) -> Optional[int]:
    hwnd = user32.FindWindowW(None, title)
    if hwnd:
        return hwnd
    result: List[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h):
            ln = user32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(ln + 1)
            user32.GetWindowTextW(h, buf, ln + 1)
            if title.lower() in buf.value.lower():
                result.append(h)
                return False
        return True
    user32.EnumWindows(cb, 0)
    return result[0] if result else None


_VK = {"enter": 0x0D, "tab": 0x09, "esc": 0x1B, "space": 0x20, "backspace": 0x08,
       "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}


def _send_keys(combo: str) -> Dict[str, Any]:
    """combo e.g. 'ctrl+c', 'alt+tab', 'enter'"""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    mods = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
    held = []
    for p in parts[:-1]:
        if p in mods:
            user32.keybd_event(mods[p], 0, 0, 0)
            held.append(mods[p])
    key = parts[-1]
    vk = _VK.get(key, ord(key.upper()) if len(key) == 1 else 0)
    if not vk:
        return {"ok": False, "error": f"unknown key: {key}"}
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 0x0002, 0)
    for m in reversed(held):
        user32.keybd_event(m, 0, 0x0002, 0)
    return {"ok": True, "combo": combo}


def _type_text(text: str) -> Dict[str, Any]:
    for ch in text:
        res = _send_keys(ch if ch.isalnum() or ch in " .,;:!?-_/\\" else "space")
        if not res.get("ok"):
            return res
    return {"ok": True, "chars": len(text)}
