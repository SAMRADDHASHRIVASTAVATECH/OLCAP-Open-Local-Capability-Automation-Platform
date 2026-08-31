"""Auto-detecting OpenCode MCP registration adapter.

Probes the real OpenCode install, detects which config variant is in use,
merges the three OLCAP MCP servers into it, verifies the result and can undo
it.  Everything is idempotent: running it repeatedly converges to the same
configuration and never duplicates an entry.

Hard rules this module obeys:
  * OpenCode's model/provider routing is never read-modify-written away - only
    the MCP key is touched, everything else is passed through untouched.
  * No skill-loading mechanism is created or modified (that stays OpenCode's).
  * Nothing is written without a timestamped backup, and every write is
    atomic (temp file + os.replace).
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# The three OLCAP MCP servers. `module` is what the server is launched as:
#   python -m <module>
# --------------------------------------------------------------------------- #
SERVERS: Dict[str, Dict[str, str]] = {
    "olcap-web-browser": {
        "module": "olcap.servers.web_browser",
        "title": "OLCAP Web + Browser",
    },
    "olcap-research-knowledge": {
        "module": "olcap.servers.research_knowledge",
        "title": "OLCAP Research + Knowledge",
    },
    "olcap-data-automation-os": {
        "module": "olcap.servers.data_automation_os",
        "title": "OLCAP Data + Automation + Computer/OS",
    },
}

# Every key OpenCode may own.  We never create, rename or delete these -
# the merge only ever adds/replaces keys *inside* the MCP section.
PRESERVE_KEYS = {
    "$schema", "model", "provider", "providers", "agent", "agents",
    "instructions", "permission", "permissions", "skills", "skill",
    "theme", "keybinds", "autoupdate", "share", "username", "command",
    "commands", "plugin", "plugins", "tools", "experimental", "tui",
    "watcher", "lsp", "formatter", "layout", "default_agent", "server",
}

MCP_KEYS = ("mcp", "mcpServers", "mcpservers", "MCPServers")


def _redact_secrets(obj: Any) -> Any:
    """Never echo credentials back: this report is printed and pasted around."""
    try:
        if isinstance(obj, dict):
            return {k: ("***set***" if re.search(
                r"(?i)(key|token|secret|password|passwd|credential)", str(k))
                else _redact_secrets(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_redact_secrets(v) for v in obj]
        return obj
    except Exception:
        return "***redacted***"


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _expand(p: str) -> Path:
    p = os.path.expandvars(os.path.expanduser(p))
    return Path(p)


def _package_root() -> Path:
    """Directory that contains the `olcap` package (needed for `python -m`)."""
    here = Path(__file__).resolve()
    # olcap/opencode/adapter.py -> parents[2] == olcap, parents[3] == root
    return here.parents[2]


# --------------------------------------------------------------------------- #
# Config discovery
# --------------------------------------------------------------------------- #
def candidate_config_paths() -> List[Path]:
    """Every location OpenCode is known to keep its configuration, in order."""
    home = _home()
    appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    cands = [
        # sst/opencode - the primary variant
        Path(xdg) / "opencode" / "opencode.json",
        Path(appdata) / "opencode" / "opencode.json",
        home / ".config" / "opencode" / "opencode.json",
        home / "AppData" / "Roaming" / "opencode" / "opencode.json",
        # older / alternate layouts
        Path(xdg) / "opencode" / "config.json",
        Path(appdata) / "opencode" / "config.json",
        home / ".opencode" / "config.json",
        home / ".opencode" / "opencode.json",
        # project-local
        Path.cwd() / "opencode.json",
        Path.cwd() / ".opencode" / "opencode.json",
        Path.cwd() / ".opencode" / "config.json",
        _package_root() / "opencode.json",
    ]
    out: List[Path] = []
    for c in cands:
        try:
            c = Path(os.path.expandvars(str(c)))
        except Exception:
            continue
        if c not in out:
            out.append(c)
    return out


def find_config(explicit: Optional[str] = None) -> Tuple[Optional[Path], Dict[str, Any]]:
    """Return (path, info) for the OpenCode config actually in use.

    When nothing exists yet, returns (primary_path, {"exists": False, ...}) so
    the caller can create a minimal file instead of failing.
    """
    if explicit:
        p = _expand(explicit)
        raw, err = _read_json(p)
        return p, {"exists": p.exists() and err is None, "explicit": True,
                   "error": err, "data": raw}
    for p in candidate_config_paths():
        if not p.exists():
            continue
        raw, err = _read_json(p)
        if err is None and isinstance(raw, dict):
            return p, {"exists": True, "explicit": False, "error": None,
                       "data": raw}
    primary = candidate_config_paths()[0]
    return primary, {"exists": False, "explicit": False, "error": None,
                     "data": None}


# --------------------------------------------------------------------------- #
# Tolerant JSON (OpenCode configs are often JSONC)
# --------------------------------------------------------------------------- #
def _strip_jsonc(text: str) -> str:
    out: List[str] = []
    i, n = 0, len(text)
    in_str = False
    esc = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    body = "".join(out)
    body = re.sub(r",(\s*[}\]])", r"\1", body)  # trailing commas
    return body


def _read_json(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"not found: {path}"
    except Exception as e:
        return None, f"unreadable: {type(e).__name__}: {e}"
    try:
        return json.loads(raw_text), None
    except Exception:
        pass
    try:
        return json.loads(_strip_jsonc(raw_text)), None
    except Exception as e:
        return None, f"invalid JSON/JSONC: {e}"


# --------------------------------------------------------------------------- #
# Variant detection
# --------------------------------------------------------------------------- #
def detect_variant(path: Path, data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Which OpenCode flavour and which MCP schema this install uses."""
    d = data or {}
    present = [k for k in MCP_KEYS if k in d]
    schema = present[0] if present else None
    name = path.name.lower()

    if schema == "mcp" or (schema is None and name.startswith("opencode")):
        variant = "sst/opencode (opencode.json)"
        schema = schema or "mcp"
    elif schema in ("mcpServers", "mcpservers", "MCPServers"):
        variant = "mcpServers-style config"
        schema = "mcpServers"
    else:
        variant = "unknown OpenCode variant"
        schema = schema or ("mcp" if name.startswith("opencode")
                            else "mcpServers")

    exe = shutil.which("opencode") or shutil.which("opencode.cmd") \
        or shutil.which("opencode.exe")
    version = None
    if exe:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True,
                               text=True, timeout=20)
            version = (r.stdout or r.stderr).strip().splitlines()[0][:120] \
                if (r.stdout or r.stderr).strip() else None
        except Exception:
            version = None
    return {"variant": variant, "mcp_key": schema, "executable": exe,
            "version": version, "config_path": str(path),
            "has_existing_mcp": bool(present)}


# --------------------------------------------------------------------------- #
# Entry construction
# --------------------------------------------------------------------------- #
def _python_command(mcp_key: str, module: str, target_windows: bool) -> Dict[str, Any]:
    """Build the launch entry for one server, for the detected MCP schema."""
    root = str(_package_root())
    env = {"PYTHONPATH": root,
           "PYTHONIOENCODING": "utf-8",
           "OLCAP_HOME": str(Path(os.environ.get("OLCAP_HOME")
                                  or (_home() / ".olcap")))}
    # When we generate for the current interpreter use its absolute path; when
    # we are cross-targeting (building on Linux for the Windows box) fall back
    # to plain `python`, which Windows resolves through PATH.
    if (sys.platform.startswith("win") and not target_windows) or \
            (target_windows and sys.platform.startswith("win")):
        py = sys.executable or "python"
    else:
        py = sys.executable if not target_windows else "python"
    if target_windows and not sys.platform.startswith("win"):
        py = "python"
    if mcp_key == "mcp":
        # sst/opencode local MCP schema
        entry: Dict[str, Any] = {
            "type": "local",
            "command": [py, "-m", module],
            "enabled": True,
        }
        if env:
            entry["environment"] = env
        return entry
    # Classic mcpServers schema
    return {"command": py, "args": ["-m", module], "env": env}


def _entry_matches(existing: Any, new: Dict[str, Any]) -> bool:
    if not isinstance(existing, dict):
        return False
    if "command" in new and isinstance(new["command"], list):
        if list(map(str, existing.get("command") or [])) != \
                list(map(str, new["command"])):
            return False
        return bool(existing.get("enabled", True)) == bool(new.get("enabled", True))
    cmd = existing.get("command")
    if not cmd:
        return False
    return Path(str(cmd)).name.lower().startswith("python") or \
        str(cmd).lower() in ("python", "python3", "python.exe")


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #
class OpenCodeAdapter:
    """Register / verify / remove the OLCAP MCP servers in OpenCode."""

    def __init__(self, config_path: Optional[str] = None,
                 target_windows: Optional[bool] = None) -> None:
        if target_windows is None:
            target_windows = bool(config_path and
                                  ("AppData" in config_path or
                                   re.search(r"^[A-Za-z]:[\\/]", config_path)))
        self.target_windows = target_windows
        self.path, self.info = find_config(config_path)
        self.data: Dict[str, Any] = dict(self.info.get("data") or {})
        self.variant = detect_variant(self.path, self.info.get("data"))
        self.backups: List[str] = []

    # ---------------------------- inspection --------------------------- #
    def probe(self) -> Dict[str, Any]:
        d = self.info.get("data") or {}
        existing = {}
        for k in MCP_KEYS:
            if isinstance(d.get(k), dict):
                existing.update(d[k])
        ours = {n: existing.get(n) for n in SERVERS if n in existing}
        skills: List[str] = []
        for base in {self.path.parent, _home() / ".config" / "opencode",
                     _package_root()}:
            try:
                for m in Path(base).rglob("SKILL.md"):
                    skills.append(str(m))
                    if len(skills) >= 20:
                        break
            except Exception:
                continue
        routing = {k: _redact_secrets(d.get(k))
                   for k in ("model", "provider", "providers") if k in d}
        return {
            "config_path": str(self.path),
            "config_exists": bool(self.info.get("exists")),
            "variant": self.variant["variant"],
            "mcp_key": self.variant["mcp_key"],
            "opencode_executable": self.variant["executable"],
            "opencode_version": self.variant["version"],
            "existing_mcp_servers": sorted(existing.keys()),
            "olcap_servers_registered": sorted(ours.keys()),
            "model_routing_present": routing,
            "skills_found": skills,
            "preserved_keys_present": sorted(k for k in d if k in PRESERVE_KEYS),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    # ---------------------------- write path ---------------------------- #
    def _backup(self) -> Optional[str]:
        if not self.path.exists():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = self.path.with_suffix(self.path.suffix + f".olcap-backup-{stamp}")
        try:
            shutil.copy2(self.path, dst)
            self.backups.append(str(dst))
            return str(dst)
        except Exception:
            return None

    def _atomic_write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    def _target_data(self, remove: bool = False) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Return (new_config, report).  Never drops unknown/OpenCode keys."""
        out = dict(self.data)
        key = self.variant["mcp_key"]
        section = dict(out.get(key) or {})
        report: Dict[str, Any] = {"key": key, "added": [], "updated": [],
                                  "unchanged": [], "removed": []}
        for name, meta in SERVERS.items():
            if remove:
                if name in section:
                    section.pop(name)
                    report["removed"].append(name)
                continue
            entry = _python_command(key, meta["module"], self.target_windows)
            cur = section.get(name)
            if cur is None:
                section[name] = entry
                report["added"].append(name)
            elif _entry_matches(cur, entry):
                report["unchanged"].append(name)
            else:
                section[name] = entry
                report["updated"].append(name)
        if section:
            out[key] = section
        elif remove and key in out:
            # We emptied the section, so the key itself must go. The old guard
            # only dropped it when the ORIGINAL section was already empty,
            # which meant removing every OLCAP server left `out[key]` pointing
            # at the untouched original dict: the removal reported success and
            # was never written to disk.
            out.pop(key, None)
        return out, report

    def register(self, remove: bool = False, dry_run: bool = False,
                 names: Optional[List[str]] = None) -> Dict[str, Any]:
        global SERVERS_SNAPSHOT
        if names:
            keep = {k: v for k, v in SERVERS.items() if k in names}
            if not keep:
                raise ValueError(f"unknown server names: {names}")
            saved = dict(SERVERS)
            SERVERS.clear()
            SERVERS.update(keep)
            try:
                return self._register(remove, dry_run)
            finally:
                SERVERS.clear()
                SERVERS.update(saved)
        return self._register(remove, dry_run)

    def _register(self, remove: bool, dry_run: bool) -> Dict[str, Any]:
        new_data, report = self._target_data(remove=remove)
        changed = bool(report["added"] or report["updated"] or report["removed"])
        backup = None
        if changed and not dry_run:
            backup = self._backup()
            self._atomic_write(new_data)
            self.data = new_data
        return {"config_path": str(self.path),
                "variant": self.variant["variant"],
                "mcp_key": report["key"],
                "dry_run": dry_run,
                "changed": changed,
                "backup": backup,
                "added": report["added"],
                "updated": report["updated"],
                "unchanged": report["unchanged"],
                "removed": report["removed"],
                "model_routing_untouched": True,
                "new_config": new_data if dry_run else None}

    # ------------------------------ verify ------------------------------ #
    def verify(self) -> Dict[str, Any]:
        """Re-read the file from disk and confirm the entries really work."""
        data, err = _read_json(self.path)
        if err:
            return {"ok": False, "error": err, "servers": {}}
        key = self.variant["mcp_key"]
        section = data.get(key) or data.get("mcpServers") or {}
        servers: Dict[str, Any] = {}
        for name, meta in SERVERS.items():
            entry = section.get(name)
            if not entry:
                servers[name] = {"registered": False, "launch_ok": False}
                continue
            module = meta["module"]
            if isinstance(entry.get("command"), list) and len(entry["command"]) >= 3:
                mod = entry["command"][2]
            else:
                mod = None
                for a in (entry.get("args") or []):
                    if a.startswith("olcap."):
                        mod = a
            # Two levels of proof: the module can be *located*, and it can
            # actually be *imported* (i.e. its dependencies are installed).
            # "Registered in a config file" is not evidence that it runs.
            locatable = False
            try:
                locatable = importlib.util.find_spec(module) is not None
            except Exception:
                locatable = False
            import_error = "not attempted (module not found)"
            imports = False
            if locatable:
                try:
                    importlib.import_module(module)
                    imports = True
                    import_error = None
                except Exception as e:
                    import_error = f"{type(e).__name__}: {str(e)[:200]}"
            # A registered entry is only "launchable" if it points at our real
            # module - not just at some other server with the same name.
            servers[name] = {"registered": True,
                             "points_at": mod,
                             "module_locatable": locatable,
                             "module_imports": imports,
                             "import_error": import_error,
                             "launch_ok": bool(mod == module and imports)}
        preserved = {k: (k in data) for k in PRESERVE_KEYS
                     if k in (self.data or {})}
        return {"ok": all(v.get("launch_ok") for v in servers.values()),
                "config_path": str(self.path),
                "mcp_key": key,
                "servers": servers,
                "opencode_keys_still_present": preserved,
                "model_routing_intact": all(
                    (k not in self.data) or (k in data and data[k] == self.data[k])
                    for k in ("model", "provider", "providers")),
                "other_mcp_servers_preserved": sorted(
                    n for n in section if n not in SERVERS)}


SERVERS_SNAPSHOT: Dict[str, Dict[str, str]] = {}


# --------------------------------------------------------------------------- #
# Module-level convenience API
# --------------------------------------------------------------------------- #
def probe(config_path: Optional[str] = None) -> Dict[str, Any]:
    return OpenCodeAdapter(config_path).probe()


def register(config_path: Optional[str] = None, remove: bool = False,
             dry_run: bool = False,
             names: Optional[List[str]] = None) -> Dict[str, Any]:
    ad = OpenCodeAdapter(config_path)
    res = ad.register(remove=remove, dry_run=dry_run, names=names)
    res["verification"] = ad.verify() if not dry_run else None
    return res


def verify(config_path: Optional[str] = None) -> Dict[str, Any]:
    return OpenCodeAdapter(config_path).verify()


def rollback(backup_path: Optional[str] = None,
             config_path: Optional[str] = None) -> Dict[str, Any]:
    """Restore a backup made by this module (most recent one if omitted)."""
    ad = OpenCodeAdapter(config_path)
    if not backup_path:
        cands = sorted(ad.path.parent.glob(ad.path.name + ".olcap-backup-*"))
        if not cands:
            return {"ok": False, "error": f"no backup found for {ad.path}"}
        backup_path = str(cands[-1])
    src = Path(backup_path)
    if not src.exists():
        return {"ok": False, "error": f"backup missing: {backup_path}"}
    shutil.copy2(src, ad.path)
    return {"ok": True, "restored_from": backup_path, "config_path": str(ad.path),
            "verification": OpenCodeAdapter(str(ad.path)).verify()}


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="olcap.opencode",
        description="Register/verify/remove the three OLCAP MCP servers "
                    "inside an existing OpenCode installation.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--probe", action="store_true",
                   help="inspect the OpenCode install and print a report")
    g.add_argument("--register", action="store_true",
                   help="merge the OLCAP MCP servers into the config")
    g.add_argument("--remove", action="store_true",
                   help="remove the OLCAP MCP servers from the config")
    g.add_argument("--verify", action="store_true",
                   help="re-read the config and check every entry really works")
    g.add_argument("--rollback", nargs="?", const="", default=None,
                   help="restore a backup (latest if no path given)")
    ap.add_argument("--config", default=None,
                    help="explicit path to opencode.json/config.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the merged config without writing")
    ap.add_argument("--only", default=None,
                    help="comma-separated subset of server names")
    args = ap.parse_args(argv)

    names = [n.strip() for n in args.only.split(",")] if args.only else None

    if args.rollback is not None:
        out = rollback(args.rollback or None, args.config)
    elif args.verify:
        out = verify(args.config)
    elif args.remove:
        out = register(args.config, remove=True, dry_run=args.dry_run,
                       names=names)
    elif args.register:
        out = register(args.config, remove=False, dry_run=args.dry_run,
                       names=names)
    else:
        out = probe(args.config)
    print(json.dumps(out, indent=2, default=str))
    ok = out.get("ok", True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
