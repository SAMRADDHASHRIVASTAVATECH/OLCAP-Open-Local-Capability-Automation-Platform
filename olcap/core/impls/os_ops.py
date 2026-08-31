"""
MCP SERVER 3 implementations - Computer/OS.

Every call goes through the permission policy before it reaches the OS adapter:
  read       -> read
  write      -> write
  delete/kill-> destructive
  terminal   -> execute
  clipboard  -> credentials (approval required)
"""
from __future__ import annotations
import shlex

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...platform.factory import get_adapter
from ..config import cfg
from ..observability import span
from ..permissions import policy
from ..runtime import implements

_DESTRUCTIVE_FS = {"delete", "rm", "remove", "move", "rename"}
_DESTRUCTIVE_PROC = {"kill", "stop", "terminate"}


def _is_windows() -> bool:
    import platform as _p
    return _p.system().lower() == "windows"


def _gate(category: str, action: str, resource: str = "") -> None:
    policy().require(action, category, resource)


def _adapter():
    return get_adapter()


# --------------------------------------------------------------------------- #
@implements("FILESYSTEM", "olcap-os-adapter")
def filesystem_op(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "list").lower()
    path = params.get("path") or ""
    ad = _adapter()
    with span("os.filesystem", "capability", {"action": action,
                                              "path": str(path)[:140]}) as sp:
        if action in ("list", "ls", "dir", "glob"):
            _gate("read", "list", path)
            entries = ad.fs_list(path or str(cfg().home), params.get("pattern", "*"),
                                 bool(params.get("recursive")),
                                 int(params.get("limit", 500) or 500))
            sp.set(entries=len(entries))
            return {"ok": True, "action": "list", "path": path, "entries": entries}
        if action in ("read", "cat", "get"):
            _gate("read", "read", path)
            content = ad.fs_read(path, limit=int(params.get("limit", 200000)))
            return {"ok": True, "action": "read", "path": path,
                    "content": content, "chars": len(content)}
        if action in ("write", "put", "save"):
            _gate("write", "write", path)
            out = ad.fs_write(path, params.get("content", ""),
                              bool(params.get("append")))
            return {"ok": True, "action": "write", **out}
        if action in ("delete", "rm", "remove"):
            _gate("destructive", "delete", path)
            return {"ok": True, **ad.fs_delete(path)}
        if action in ("move", "rename"):
            _gate("destructive", "move", path)
            return {"ok": True, **ad.fs_move(path, params.get("destination", ""))}
        if action in ("copy", "cp"):
            _gate("write", "copy", params.get("destination", ""))
            return {"ok": True, **ad.fs_copy(path, params.get("destination", ""))}
        if action in ("stat", "info", "exists"):
            _gate("read", "stat", path)
            return {"ok": True, **ad.fs_stat(path)}
        if action == "mkdir":
            _gate("write", "mkdir", path)
            Path(path).mkdir(parents=True, exist_ok=True)
            return {"ok": True, "path": path}
        raise ValueError(f"unknown filesystem action: {action}")


@implements("TERMINAL", "olcap-os-adapter")
def terminal_run(params: Dict[str, Any]) -> Dict[str, Any]:
    command = params.get("command") or ""
    if not command:
        raise ValueError("command is required")
    _gate("execute", "terminal", command[:200])
    # A command string is executed WITHOUT a shell unless the caller opts in
    # with shell=true. Shell metacharacters in model- or page-generated text
    # (`;`, `&&`, `|`, backticks, $(...)) then arrive as literal arguments
    # instead of running extra commands.
    want_shell = str(params.get("shell", "")).lower() in ("1", "true", "yes")
    if want_shell:
        from .. import state as _state          # local import: os_ops is
        _state.emit("terminal.shell_requested", "security",
                    {"command": command[:160]})  # imported early at startup
        out = _adapter().terminal(command, cwd=params.get("cwd"),
                                  timeout_s=int(params.get("timeout_s", 120)),
                                  env=params.get("env"), shell=True)
    else:
        try:
            argv = shlex.split(command, posix=not _is_windows())
        except ValueError as e:
            raise ValueError(f"cannot parse command safely: {e}")
        if not argv:
            raise ValueError("empty command")
        out = _adapter().terminal(argv, cwd=params.get("cwd"),
                                  timeout_s=int(params.get("timeout_s", 120)),
                                  env=params.get("env"), shell=False)
    with span("os.terminal", "capability", {"command": command[:160],
                                            "shell": want_shell}) as sp:
        pass
        sp.set(exit_code=out.get("exit_code"))
    return {"ok": out.get("ok", False), **out, "command": command[:400]}


@implements("PROCESS_CONTROL", "olcap-os-adapter")
def process_control(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "list").lower()
    ad = _adapter()
    if action == "list":
        _gate("read", "process_list")
        procs = ad.process_list(params.get("name", ""),
                                int(params.get("limit", 100) or 100))
        return {"ok": True, "processes": procs, "count": len(procs)}
    if action == "start":
        argv = params.get("argv") or []
        if not argv:
            raise ValueError("argv required")
        _gate("execute", "process_start", " ".join(map(str, argv))[:200])
        return {"ok": True, **ad.process_start([str(a) for a in argv],
                                               cwd=params.get("cwd"),
                                               env=params.get("env"))}
    if action in _DESTRUCTIVE_PROC:
        pid = int(params.get("pid") or 0)
        _gate("destructive", "process_kill", f"pid:{pid}")
        return {"ok": True, **ad.process_kill(pid, force=action != "terminate")}
    if action in ("info", "capabilities"):
        return {"ok": True, **ad.capabilities()}
    raise ValueError(f"unknown process action: {action}")


@implements("WINDOWS_CONTROL", "olcap-os-adapter")
def windows_control(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "info").lower()
    p = params.get("params") or {}
    if action in ("env_set", "registry_set", "service_control"):
        _gate("write" if action == "env_set" else "destructive", action,
              str(p.get("key") or p.get("name") or p.get("path", "")))
    elif action in ("clipboard_get", "clipboard_set"):
        _gate("credentials", action, "clipboard")
    elif action in ("read", "env_get", "info", "capabilities", "services", "which"):
        _gate("read", action)
    with span("os.control", "capability", {"action": action}) as sp:
        out = _adapter().os_control(action, p)
        sp.set(ok=out.get("ok"))
    return {"ok": out.get("ok", False), "action": action, "platform": cfg().platform,
            "data": {k: v for k, v in out.items() if k != "ok"}}


@implements("GUI", "olcap-os-adapter")
def gui_action(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "").lower()
    _gate("execute", f"gui_{action}")
    out = _adapter().gui_action(action, params.get("params") or params)
    return {"ok": out.get("ok", False), "action": action, **out}


@implements("SCREENSHOT", "olcap-os-adapter")
def screenshot_capture(params: Dict[str, Any]) -> Dict[str, Any]:
    path = params.get("path") or ""
    _gate("write", "screenshot", path)
    _gate("read", "screenshot_screen")
    out = _adapter().screenshot(params.get("target", "screen"),
                                params.get("region"), path)
    return {"ok": out.get("ok", False), **out}


@implements("SCREENSHOT", "pillow")
def screenshot_pillow(params: Dict[str, Any]) -> Dict[str, Any]:
    """Screenshot through Pillow.

    PIL.ImageGrab captures a real screen on Windows and macOS (the delivery
    target is Windows).  On a headless Linux box there is no screen to grab,
    so this says so plainly instead of pretending it captured something --
    the runtime then falls through to the playwright/OS-adapter backends.
    """
    import platform as _plat

    path = params.get("path") or ""
    if not path:
        raise ValueError("path is required for screenshot")
    _gate("write", "screenshot", path)

    sysname = _plat.system().lower()
    if sysname not in ("windows", "darwin"):
        raise RuntimeError(
            "PIL.ImageGrab can only capture a screen on Windows/macOS; this "
            "machine has no screen to grab (headless Linux) - use the "
            "playwright or OS-adapter backend instead")

    try:
        from PIL import ImageGrab  # type: ignore
    except Exception as e:          # pragma: no cover - dependency missing
        raise RuntimeError(f"pillow is not importable: {type(e).__name__}: {e}")

    region = params.get("region")
    bbox = None
    if isinstance(region, dict):
        bbox = (int(region.get("x", 0)), int(region.get("y", 0)),
                int(region.get("x", 0)) + int(region.get("width", 0)),
                int(region.get("y", 0)) + int(region.get("height", 0)))
    elif isinstance(region, (list, tuple)) and len(region) == 4:
        bbox = tuple(int(v) for v in region)

    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    img = ImageGrab.grab(bbox=bbox) if bbox else ImageGrab.grab()
    img.save(str(p))
    if not p.exists() or p.stat().st_size == 0:
        return {"ok": False, "error": f"screenshot not written to {p}"}
    return {"ok": True, "path": str(p), "width": img.width, "height": img.height,
            "bytes": p.stat().st_size, "backend": "pillow",
            "target": params.get("target", "screen")}


@implements("WINDOW_MANAGEMENT", "olcap-os-adapter")
def window_manage(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "list").lower()
    if action in ("close",):
        _gate("destructive", "window_close", params.get("title", ""))
    else:
        _gate("execute", f"window_{action}", params.get("title", ""))
    out = _adapter().window_manage(action, params.get("title", ""),
                                   params.get("params") or {})
    return {"ok": out.get("ok", False), "action": action, **out}


@implements("COMPUTER_USE", "olcap-os-adapter")
def computer_use(params: Dict[str, Any]) -> Dict[str, Any]:
    """Composite: observe the machine, then act on it, with one audit trail."""
    action = (params.get("action") or "observe").lower()
    p = params.get("params") or {}
    ad = _adapter()
    if action == "observe":
        return {"ok": True, "observation": {
            "info": ad.info(), "capabilities": ad.capabilities(),
            "windows": ad.window_manage("list").get("windows", [])
            if ad.capabilities().get("window_management") else [],
            "cwd": os.getcwd()}}
    if action == "screenshot":
        return screenshot_capture(p)
    if action in ("click", "type", "key", "move", "scroll"):
        return gui_action({"action": action, "params": p})
    if action in ("focus", "window"):
        return window_manage({"action": p.get("op", "focus"), "title": p.get("title", "")})
    if action == "run":
        return terminal_run(p)
    if action == "open":
        _gate("execute", "open", str(p.get("path") or p.get("url", "")))
        out = ad.os_control("open", p)
        return {"ok": out.get("ok", False), **out}
    return {"ok": False, "error": f"unknown computer_use action: {action}"}


# --------------------------------------------------------------------------- #
@implements("OBSERVABILITY", "olcap-observability")
def observability_report(params: Dict[str, Any]) -> Dict[str, Any]:
    from .. import state
    from ..health import summary as health_summary
    from ..jit import jit as jit_mod
    from ..observability import counters, recent_spans
    scope = (params.get("scope") or "summary").lower()
    out: Dict[str, Any] = {"counters": counters()}
    if scope in ("summary", "health", "all"):
        out["health"] = health_summary()
    if scope in ("traces", "all"):
        out["spans"] = recent_spans(200)
    if scope in ("jit", "all"):
        out["jit"] = jit_mod().pool_state()
    if scope == "all":
        out["events"] = state.events(200)
    return {"ok": True, "scope": scope, **out}


@implements("VERIFICATION", "olcap-verification")
def verify_result(params: Dict[str, Any]) -> Dict[str, Any]:
    from ..verification import verify_artifact
    return verify_artifact(params)


@implements("ROUTING", "olcap-deterministic-router")
def routing_model_op(params: Dict[str, Any]) -> Dict[str, Any]:
    from ..router import router
    action = (params.get("action") or "status").lower()
    r = router()
    if action == "train":
        return {"ok": True, "status": "trained", **r.train()}
    if action == "status":
        return {"ok": True, **r.status()}
    if action == "disable":
        return {"ok": True, **r.disable()}
    if action == "candidates":
        from ..registry import registry
        return {"ok": True,
                "candidates": registry().candidates(params.get("capability", ""))}
    return {"ok": False, "error": f"unknown routing action: {action}"}
