"""
AUTOMATIC COMPONENT MANAGER.

discover -> verify -> resolve dependencies -> install -> configure ->
integrate -> register -> start -> functionally test -> health check -> ready

Before installing anything it verifies: canonical repository, project identity,
official documentation, license, maintenance, version, dependencies, install
method, Windows compatibility, runtime requirements, API requirements, cloud
requirements, paid requirements, security and resource requirements.

Idempotent: an already-correct component is detected, validated and reused.
Supports install / configure / build / start / stop / restart / update /
remove / enable / disable / validate / repair / roll back / health-check /
register / fallback.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core import state
from ..core.config import cfg
from ..core.health import functional_check, probe
from ..core.models import HealthState, InstallMethod
from ..core.observability import span
from ..core.registry import registry
from .provenance import verify_component

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = cfg().data / "component_manifest.json"


def _pip(args: List[str], timeout: int = 900) -> Dict[str, Any]:
    cmd = [sys.executable, "-m", "pip", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {"ok": r.returncode == 0, "rc": r.returncode,
            "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:], "cmd": " ".join(cmd)}


def _npm(args: List[str], timeout: int = 900) -> Dict[str, Any]:
    r = subprocess.run(["npm", *args], capture_output=True, text=True, timeout=timeout,
                       shell=cfg().is_windows)
    return {"ok": r.returncode == 0, "rc": r.returncode,
            "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]}


def _run(cmd: List[str], timeout: int = 600) -> Dict[str, Any]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {"ok": r.returncode == 0, "rc": r.returncode,
            "stdout": r.stdout[-4000:], "stderr": r.stderr[-2000:]}


class ComponentManager:
    def __init__(self) -> None:
        self.c = cfg()
        self.reg = registry()
        self.history: List[Dict[str, Any]] = []
        self._load_manifest()

    # ------------------------------------------------------------------ #
    def _load_manifest(self) -> None:
        if MANIFEST.exists():
            try:
                self.manifest = json.loads(MANIFEST.read_text())
            except Exception:
                self.manifest = {}
        else:
            self.manifest = {}

    def _save_manifest(self) -> None:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(self.manifest, indent=2))

    def _record(self, cid: str, action: str, outcome: Dict[str, Any]) -> None:
        rec = {"component": cid, "action": action, "ts": time.time(), **outcome}
        self.history.append(rec)
        self.manifest.setdefault(cid, {}).update({
            "last_action": action,
            "last_action_ts": rec["ts"],
            "last_ok": bool(outcome.get("ok")),
        })
        self._save_manifest()
        state.emit(f"component.{action}", "component_manager",
                   {"component": cid, "ok": outcome.get("ok")})

    # ------------------------------------------------------------------ #
    # VERIFY (pre-install inspection)
    # ------------------------------------------------------------------ #
    def verify(self, cid: str, check_upstream: bool = True) -> Dict[str, Any]:
        comp = self.reg.component(cid)
        if not comp:
            return {"ok": False, "error": f"unknown component {cid}"}
        out: Dict[str, Any] = {
            "id": cid, "name": comp.name,
            "canonical_repository": comp.repository or "(builtin)",
            "documentation": comp.documentation,
            "owner": comp.owner, "license": comp.license,
            "version": comp.version, "maintenance": comp.maintenance,
            "install_method": comp.install_method.value,
            "platforms": comp.platforms,
            "windows_support": "windows" in comp.platforms,
            "linux_support": "linux" in comp.platforms,
            "runtimes": comp.runtimes,
            "api_requirements": comp.api_requirements,
            "cloud_requirements": comp.cloud_requirements,
            "paid": comp.paid, "paid_note": comp.paid_note,
            "self_hosted": comp.self_hosted,
            "resource_mb": comp.resource_mb,
            "jit": comp.jit,
            "mandatory": not comp.optional,
        }
        if check_upstream and comp.repository:
            prov = verify_component(cid)
            out["provenance"] = {k: prov.get(k) for k in
                                 ("verified", "api_license", "license_matches",
                                  "stars", "archived", "days_since_push",
                                  "maintenance_state", "html_url", "acceptable")}
            out["upstream_ok"] = bool(prov.get("acceptable"))
        else:
            out["upstream_ok"] = True
            out["provenance"] = {"verified": None, "note": "local/builtin"}

        # A paid component may never be mandatory.
        if comp.paid and not comp.optional:
            out["policy_violation"] = "paid component marked mandatory"
        out["installable_here"] = self._installable(comp)
        out["ok"] = bool(out["upstream_ok"] and
                         comp.supports(self.c.platform) and
                         not out.get("policy_violation"))
        return out

    def _installable(self, comp) -> Dict[str, Any]:
        m = comp.install_method
        if m == InstallMethod.BUILTIN:
            return {"ok": True, "reason": "builtin"}
        if m == InstallMethod.PYTHON:
            return {"ok": True if shutil.which("pip") or sys.executable else False,
                    "reason": "pip"}
        if m == InstallMethod.NODE:
            return {"ok": shutil.which("npm") is not None, "reason": "npm"}
        if m == InstallMethod.DOCKER:
            return {"ok": shutil.which("docker") is not None, "reason": "docker"}
        if m == InstallMethod.BINARY:
            return {"ok": True, "reason": "manual/binary download"}
        return {"ok": True, "reason": "service"}

    # ------------------------------------------------------------------ #
    # INSTALL
    # ------------------------------------------------------------------ #
    def install(self, cid: str, force: bool = False,
                verify_upstream: bool = True) -> Dict[str, Any]:
        comp = self.reg.component(cid)
        if not comp:
            return {"ok": False, "error": f"unknown component {cid}"}

        current = probe(comp)
        if current.installed and not force:
            self.reg.set_health(cid, current)
            out = {"ok": True, "action": "reused", "installed": True,
                   "detail": "already installed and validated"}
            self._record(cid, "install", out)
            return out

        if not comp.supports(self.c.platform):
            out = {"ok": False, "action": "skipped",
                   "error": f"not supported on {self.c.platform}"}
            self._record(cid, "install", out)
            return out

        pre = self.verify(cid, check_upstream=verify_upstream)
        if not pre.get("ok") and not comp.optional:
            out = {"ok": False, "action": "blocked", "verification": pre}
            self._record(cid, "install", out)
            return out

        with span("component.install", "manager", {"component": cid}) as sp:
            if comp.install_method == InstallMethod.BUILTIN:
                res = {"ok": True, "stdout": "builtin - nothing to install"}
            elif comp.install_method == InstallMethod.PYTHON:
                res = _pip(["install", "--disable-pip-version-check",
                            comp.install_target, *comp.install_args])
            elif comp.install_method == InstallMethod.NODE:
                res = _npm(["install", "-g", comp.install_target, *comp.install_args])
            elif comp.install_method == InstallMethod.DOCKER:
                res = _run(["docker", "pull", comp.install_target])
            else:
                res = {"ok": True, "stdout": f"manual setup required: "
                                             f"{comp.documentation}"}

            if res.get("ok"):
                for step in getattr(comp, "post_install", []) or []:
                    if step.startswith("playwright"):
                        _run([sys.executable, "-m", *step.split()], timeout=1200)
                    else:
                        _run(step.split(), timeout=900)

        after = functional_check(comp)
        self.reg.set_health(cid, after)
        out = {"ok": bool(res.get("ok")) and after.installed,
               "action": "installed", "install_result": res,
               "health": after.model_dump()}
        sp.set(ok=out["ok"])
        self._record(cid, "install", out)
        return out

    # ------------------------------------------------------------------ #
    def remove(self, cid: str) -> Dict[str, Any]:
        comp = self.reg.component(cid)
        if not comp:
            return {"ok": False, "error": "unknown component"}
        if comp.install_method == InstallMethod.PYTHON and comp.install_target:
            res = _pip(["uninstall", "-y", comp.install_target.split("[")[0]])
        elif comp.install_method == InstallMethod.NODE:
            res = _npm(["uninstall", "-g", comp.install_target])
        else:
            res = {"ok": True, "stdout": "nothing to uninstall (builtin/service)"}
        rep = probe(comp)
        self.reg.set_health(cid, rep)
        out = {"ok": bool(res.get("ok")), "action": "removed", **res}
        self._record(cid, "remove", out)
        return out

    def repair(self, cid: str) -> Dict[str, Any]:
        """Repair: re-verify, reinstall if missing, re-run health check."""
        comp = self.reg.component(cid)
        if not comp:
            return {"repaired": False, "error": "unknown component"}
        before = probe(comp)
        if before.installed and before.state == HealthState.HEALTHY:
            return {"repaired": False, "already_healthy": True}
        res = self.install(cid, force=True, verify_upstream=False)
        after = functional_check(comp)
        self.reg.set_health(cid, after)
        out = {"repaired": bool(after.installed), "install": res,
               "health": after.model_dump()}
        self._record(cid, "repair", out)
        return out

    def rollback(self, cid: str) -> Dict[str, Any]:
        """Remove and reinstall the last known-good state (best effort)."""
        self.remove(cid)
        res = self.install(cid, force=True, verify_upstream=False)
        out = {"rolled_back": bool(res.get("ok")), **res}
        self._record(cid, "rollback", out)
        return out

    def update(self, cid: str) -> Dict[str, Any]:
        comp = self.reg.component(cid)
        if not comp:
            return {"ok": False, "error": "unknown component"}
        if comp.install_method == InstallMethod.PYTHON:
            res = _pip(["install", "--upgrade", "--disable-pip-version-check",
                        comp.install_target])
        elif comp.install_method == InstallMethod.NODE:
            res = _npm(["update", "-g", comp.install_target])
        elif comp.install_method == InstallMethod.DOCKER:
            res = _run(["docker", "pull", comp.install_target])
        else:
            res = {"ok": True, "stdout": "builtin"}
        after = functional_check(comp)
        self.reg.set_health(cid, after)
        out = {"ok": bool(res.get("ok")), **res, "health": after.model_dump()}
        self._record(cid, "update", out)
        return out

    # ------------------------------------------------------------------ #
    def enable(self, cid: str, value: bool = True) -> Dict[str, Any]:
        comp = self.reg.component(cid)
        if not comp:
            return {"ok": False, "error": "unknown component"}
        comp.enabled = value
        out = {"ok": True, "component": cid, "enabled": value}
        self._record(cid, "enable" if value else "disable", out)
        return out

    def start(self, cid: str) -> Dict[str, Any]:
        comp = self.reg.component(cid)
        if not comp:
            return {"ok": False, "error": "unknown component"}
        if comp.install_method == InstallMethod.DOCKER:
            name = f"olcap-{cid}"
            _run(["docker", "rm", "-f", name], timeout=120)
            res = _run(["docker", "run", "-d", "--name", name, "-P",
                        comp.install_target], timeout=600)
            out = {"ok": bool(res.get("ok")), **res}
        else:
            rep = functional_check(comp)
            self.reg.set_health(cid, rep)
            out = {"ok": rep.installed, "health": rep.model_dump()}
        self._record(cid, "start", out)
        return out

    def stop(self, cid: str) -> Dict[str, Any]:
        res = _run(["docker", "stop", f"olcap-{cid}"], timeout=180)
        out = {"ok": bool(res.get("ok")), **res}
        self._record(cid, "stop", out)
        return out

    def restart(self, cid: str) -> Dict[str, Any]:
        self.stop(cid)
        return self.start(cid)

    # ------------------------------------------------------------------ #
    def validate(self, cid: str) -> Dict[str, Any]:
        comp = self.reg.component(cid)
        if not comp:
            return {"ok": False, "error": "unknown component"}
        rep = functional_check(comp)
        self.reg.set_health(cid, rep)
        return {"ok": rep.installed and rep.state in
                (HealthState.HEALTHY, HealthState.DEGRADED),
                "health": rep.model_dump()}

    def health_check(self, cid: str) -> Dict[str, Any]:
        comp = self.reg.component(cid)
        if not comp:
            return {"ok": False, "error": "unknown component"}
        rep = functional_check(comp)
        self.reg.set_health(cid, rep)
        return rep.model_dump()

    # ------------------------------------------------------------------ #
    def install_all(self, only_required: bool = True,
                    include_optional: Optional[List[str]] = None) -> Dict[str, Any]:
        include_optional = include_optional or []
        results: Dict[str, Any] = {}
        for cid, comp in self.reg.components.items():
            if not comp.enabled:
                results[cid] = {"ok": True, "action": "skipped", "reason": "disabled"}
                continue
            if only_required and comp.optional and cid not in include_optional:
                rep = probe(comp)
                self.reg.set_health(cid, rep)
                results[cid] = {"ok": True, "action": "skipped",
                                "reason": "optional (JIT on demand)"}
                continue
            results[cid] = self.install(cid)
        ok = sum(1 for r in results.values() if r.get("ok"))
        return {"installed": ok, "total": len(results), "results": results}

    def report(self) -> Dict[str, Any]:
        rows = {}
        for cid in self.reg.components:
            h = self.reg.health_of(cid)
            rows[cid] = {"state": h.state.value, "installed": h.installed,
                         "configured": h.configured, "jit_ready": h.jit_ready,
                         "detail": h.detail}
        return {"components": rows,
                "history": self.history[-50:],
                "manifest": self.manifest}


_CM: Optional[ComponentManager] = None


def manager() -> ComponentManager:
    global _CM
    if _CM is None:
        _CM = ComponentManager()
    return _CM


def install(cid: str, **kw) -> Dict[str, Any]:
    return manager().install(cid, **kw)


def repair(cid: str) -> Dict[str, Any]:
    return manager().repair(cid)
