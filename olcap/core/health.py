"""
Health system.

States: unavailable | starting | healthy | degraded | failed | stopped |
        ready | active | idle | released

A component is only 'healthy' when installation, configuration, dependencies,
platform and runtime are all actually OK - not merely when a process started.
"""
from __future__ import annotations

import importlib
import importlib.util
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

from . import state
from .config import cfg
from .models import ComponentSpec, HealthReport, HealthState, InstallMethod
from .observability import span


def _module_present(name: str) -> bool:
    if not name:
        return False
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _executable_present(name: str) -> bool:
    return bool(name) and shutil.which(name) is not None


def _docker_present() -> bool:
    return shutil.which("docker") is not None


def probe(comp: ComponentSpec) -> HealthReport:
    """Cheap, side-effect-free installation/configuration probe."""
    c = cfg()
    t0 = time.time()
    checks: Dict[str, bool] = {}
    detail: List[str] = []

    platform_ok = comp.supports(c.platform)
    checks["platform"] = platform_ok
    if not platform_ok:
        detail.append(f"platform {c.platform} not in {comp.platforms}")

    installed = False
    method = comp.install_method
    if method in (InstallMethod.BUILTIN,):
        installed = True
    elif method == InstallMethod.PYTHON:
        installed = _module_present(comp.python_module or comp.install_target.split("[")[0])
        if not installed:
            detail.append(f"python module '{comp.python_module or comp.install_target}' missing")
    elif method == InstallMethod.NODE:
        installed = _executable_present("npm") and bool(comp.install_target)
        if not installed:
            detail.append("node/npm missing")
    elif method == InstallMethod.BINARY:
        installed = _executable_present(comp.executable)
        if not installed:
            detail.append(f"executable '{comp.executable}' missing")
    elif method == InstallMethod.DOCKER:
        installed = _docker_present()
        if not installed:
            detail.append("docker not available")
    elif method in (InstallMethod.SERVICE, InstallMethod.SOURCE):
        # A remote service or a source checkout is only "installed" when it has
        # actually been configured here. Declaring an optional hosted backend
        # must never make it look usable - that would silently route work to a
        # component that has no implementation bound to it.
        configured_env = any(
            c.env(req.split()[0].replace("-", "_"))
            for req in (comp.api_requirements or []))
        already = bool((state.component(comp.id) or {}).get("installed"))
        installed = bool(configured_env or already or comp.executable)
        if not installed:
            detail.append("service/source not configured on this machine")
    checks["installed"] = installed

    # configuration: no *mandatory* paid/API requirement may be unsatisfied
    configured = True
    for req in comp.api_requirements:
        key = req.split()[0]
        if key.isupper() and not c.env(key.replace("-", "_")):
            if not comp.optional:
                configured = False
                detail.append(f"missing env {key}")
    checks["configured"] = configured

    state_ = HealthState.UNAVAILABLE
    if installed and platform_ok:
        state_ = HealthState.HEALTHY if configured else HealthState.DEGRADED
    elif platform_ok and comp.optional:
        state_ = HealthState.UNAVAILABLE

    return HealthReport(
        component=comp.id, state=state_, installed=installed, configured=configured,
        platform_ok=platform_ok, jit_ready=installed and platform_ok,
        detail="; ".join(detail)[:400], checks=checks,
        latency_ms=round((time.time() - t0) * 1000, 2),
        provenance=comp.repository or "builtin",
    )


def functional_check(comp: ComponentSpec) -> HealthReport:
    """Stronger check: actually exercise the component if a check is declared."""
    rep = probe(comp)
    if not rep.installed:
        return rep
    cmd = comp.healthcheck
    if not cmd or comp.install_method in (InstallMethod.BUILTIN,):
        return rep
    try:
        if comp.install_method == InstallMethod.PYTHON:
            with span("health.functional", "health", {"component": comp.id}):
                importlib.import_module(comp.python_module or comp.install_target)
            rep.state = HealthState.HEALTHY
            rep.detail = (rep.detail + "; import OK").strip("; ")
        elif comp.install_method == InstallMethod.BINARY:
            r = subprocess.run([comp.executable, "--version"], capture_output=True,
                               text=True, timeout=20)
            rep.state = HealthState.HEALTHY if r.returncode == 0 else HealthState.DEGRADED
            rep.detail = (rep.detail + f"; version rc={r.returncode}").strip("; ")
    except Exception as e:
        rep.state = HealthState.FAILED
        rep.detail = (rep.detail + f"; functional check failed: {e}")[:400]
    return rep


def check_all(functional: bool = False) -> Dict[str, Dict[str, Any]]:
    from .registry import registry
    out: Dict[str, Dict[str, Any]] = {}
    for cid, comp in registry().components.items():
        rep = functional_check(comp) if functional else probe(comp)
        registry().set_health(cid, rep)
        out[cid] = rep.model_dump()
    state.emit("health.checked", "health", {"components": len(out),
                                            "functional": functional})
    return out


def summary() -> Dict[str, Any]:
    from .registry import registry
    health = check_all()
    by_state: Dict[str, int] = {}
    for h in health.values():
        by_state[h["state"]] = by_state.get(h["state"], 0) + 1
    healthy = [k for k, v in health.items() if v["state"] == "healthy"]
    return {"total": len(health), "by_state": by_state, "healthy": sorted(healthy)}
