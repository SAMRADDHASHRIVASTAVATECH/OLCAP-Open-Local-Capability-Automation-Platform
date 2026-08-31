"""
CAPABILITY REGISTRY - one central registry for all three MCP servers.

Question:  "What capability do I need?"
Answer:    "Which available implementation can provide it?"  (never the user's job)

Selection is deterministic-first and constraint-driven:
  platform support -> enabled -> installed -> healthy -> permissions ->
  resource fit -> declared priority -> learned (RF) preference.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from .config import cfg
from .models import (CapabilitySpec, ComponentSpec, HealthReport, HealthState,
                     InstallMethod, PermissionCategory)
from . import state


# How long a stored health row may be trusted before it is re-probed.
_HEALTH_ROW_TTL_S = 300.0


class Registry:
    def __init__(self) -> None:
        self.c = cfg()
        self.capabilities: Dict[str, CapabilitySpec] = {}
        self.components: Dict[str, ComponentSpec] = {}
        self.health: Dict[str, HealthReport] = {}
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        caps = (self.c.section("capabilities").get("capabilities") or [])
        for raw in caps:
            perms = [PermissionCategory(p) for p in (raw.get("permissions") or [])]
            spec = CapabilitySpec(
                id=raw["id"], name=raw.get("name", raw["id"]),
                server=raw.get("server", ""), tool=raw.get("tool", raw["id"].lower()),
                category=raw.get("category", "general"),
                description=raw.get("description", ""),
                inputs=raw.get("inputs") or {}, outputs=raw.get("outputs") or {},
                implementations=raw.get("implementations") or [],
                fallback=raw.get("fallback") or [],
                optional_backends=raw.get("optional_backends") or [],
                permissions=perms,
                platforms=raw.get("platforms") or ["windows", "linux"],
                jit=bool(raw.get("jit", True)),
                verification_required=bool(raw.get("verification_required", False)),
            )
            self.capabilities[spec.id] = spec

        comps = (self.c.section("components").get("components") or [])
        for raw in comps:
            try:
                spec = ComponentSpec(**raw)
            except Exception as e:  # keep the registry resilient
                state.emit("registry.component.error", "registry",
                           {"id": raw.get("id"), "error": str(e)})
                continue
            self.components[spec.id] = spec
            # virtual builtins that are always present
        for vid, name in (("olcap-observability", "OLCAP Observability"),
                          ("olcap-verification", "OLCAP Verification"),
                          ("olcap-deterministic-router", "OLCAP Deterministic Router")):
            if vid not in self.components:
                self.components[vid] = ComponentSpec(
                    id=vid, name=name, category="builtin", capabilities=[],
                    license="Apache-2.0", owner="olcap", install_method=InstallMethod.BUILTIN,
                    jit=False, enabled=True, self_hosted=True, paid=False)

    # ------------------------------------------------------------------ #
    def reload(self) -> None:
        self.capabilities.clear(); self.components.clear()
        self.c = cfg(); self._load()

    # ------------------------------------------------------------------ #
    def capability(self, cid: str) -> Optional[CapabilitySpec]:
        return self.capabilities.get(cid.upper())

    def component(self, cid: str) -> Optional[ComponentSpec]:
        return self.components.get(cid)

    def capabilities_for_server(self, server: str) -> List[CapabilitySpec]:
        return [c for c in self.capabilities.values() if c.server == server]

    def tools_for_server(self, server: str) -> List[str]:
        return sorted({c.tool for c in self.capabilities_for_server(server)})

    # ------------------------------------------------------------------ #
    def set_health(self, cid: str, report: HealthReport) -> None:
        self.health[cid] = report
        state.set_component(cid, state=report.state.value,
                            installed=int(report.installed),
                            configured=int(report.configured),
                            health=report.state.value,
                            detail=report.detail[:500])

    def health_of(self, cid: str) -> HealthReport:
        """Lazily probe on first ask so no bootstrap step is ever required."""
        if cid in self.health:
            return self.health[cid]
        row = state.component(cid) or {}
        if row:
            # A stored row is a cache, not a verdict. Health is meant to be
            # live: a component installed *after* the last sweep (or written
            # as unavailable ages ago) must be re-probed, otherwise the stale
            # row permanently hides a backend that is ready right now.
            age = (time.time() - float(row.get("updated_at") or 0))
            stale = age > _HEALTH_ROW_TTL_S or not row.get("installed")
            if not stale:
                return HealthReport(component=cid,
                                    state=HealthState(row.get("state", "unavailable")),
                                    installed=bool(row.get("installed")),
                                    configured=bool(row.get("configured")),
                                    detail=row.get("detail", "") or "")
        comp = self.components.get(cid)
        if comp is None:
            return HealthReport(component=cid, detail="unknown component")
        from .health import probe
        rep = probe(comp)
        self.set_health(cid, rep)
        return rep

    # ------------------------------------------------------------------ #
    def _usable(self, comp: ComponentSpec, platform: str,
                allow_not_installed: bool = False) -> Tuple[bool, str]:
        if not comp.enabled:
            return False, "disabled"
        if platform and not comp.supports(platform):
            return False, f"platform {platform} unsupported"
        h = self.health_of(comp.id)
        if not allow_not_installed:
            if not h.installed:
                return False, "not installed"
            if h.state in (HealthState.FAILED, HealthState.UNAVAILABLE):
                return False, f"health={h.state.value}"
        return True, ""

    def has_code(self, capability_id: str, comp_id: str) -> bool:
        """Is there actually an implementation behind this component?

        A declared backend with no code is worse than no backend: the runtime
        would pick it and then fail with "no implementation bound". This is
        the guard that keeps declared-but-unwired optional backends inert.
        """
        try:
            from .runtime import IMPLS
            if (str(capability_id).upper(), comp_id) in IMPLS:
                return True
            from .worker import LOADERS
            handler = LOADERS.get(comp_id)
            if handler is None:
                return False
            cap = self.capability(capability_id)
            tool = (cap.tool if cap else str(capability_id).lower())
            return hasattr(handler, tool) or hasattr(handler, "ping")
        except Exception:
            # cannot prove absence -> do not block on an inspection error
            return True

    def select(self, capability_id: str, platform: Optional[str] = None,
               required_permissions: Optional[List[PermissionCategory]] = None,
               learned_preference: Optional[str] = None,
               exclude: Optional[List[str]] = None,
               allow_not_installed: bool = False) -> Optional[ComponentSpec]:
        """Return the best usable implementation for a capability."""
        cap = self.capability(capability_id)
        if not cap:
            return None
        platform = platform or self.c.platform
        exclude = set(exclude or [])
        ordered = list(cap.implementations)

        # Learned preference can reorder, but never outside the declared set.
        if learned_preference and learned_preference in ordered:
            ordered.remove(learned_preference)
            ordered.insert(0, learned_preference)

        for cid in ordered:
            if cid in exclude:
                continue
            comp = self.components.get(cid)
            if not comp or not comp.id:
                continue
            if not self.has_code(capability_id, cid):
                continue
            ok, _why = self._usable(comp, platform, allow_not_installed)
            if not ok:
                continue
            if required_permissions:
                missing = [p.value for p in required_permissions
                           if p not in comp.permissions]
                if missing:
                    continue
            return comp

        # fallbacks (including components that declare fallback_for)
        for cid in cap.fallback:
            if cid in exclude:
                continue
            comp = self.components.get(cid)
            if comp and self._usable(comp, platform, allow_not_installed)[0] \
                    and self.has_code(capability_id, cid):
                return comp
        wanted = str(capability_id or "").upper()
        for comp in self.components.values():
            if comp.id in exclude:
                continue
            # Case-insensitive: config files are hand-editable and a stray
            # lowercase id must never silently disable this fallback path.
            declared = [str(x).upper() for x in (comp.capabilities or [])
                        + (comp.provides or [])]
            if wanted in declared or capability_id in (comp.fallback_for or []):
                if self._usable(comp, platform, allow_not_installed)[0]:
                    return comp
        return None

    def candidates(self, capability_id: str, platform: Optional[str] = None
                   ) -> List[Dict[str, Any]]:
        cap = self.capability(capability_id)
        if not cap:
            return []
        platform = platform or self.c.platform
        out = []
        for cid in cap.implementations + cap.fallback:
            comp = self.components.get(cid)
            if not comp:
                out.append({"id": cid, "usable": False, "reason": "unknown component"})
                continue
            ok, why = self._usable(comp, platform)
            h = self.health_of(cid)
            out.append({"id": cid, "usable": ok, "reason": why,
                        "has_code": self.has_code(capability_id, cid),
                        "installed": h.installed, "state": h.state.value,
                        "paid": comp.paid, "self_hosted": comp.self_hosted,
                        "jit": comp.jit, "resource_mb": comp.resource_mb})
        return out

    # ------------------------------------------------------------------ #
    def provenance(self, cid: str) -> Dict[str, Any]:
        comp = self.components.get(cid)
        if not comp:
            return {}
        h = self.health_of(cid)
        row = state.component(cid) or {}
        return {
            "id": comp.id, "name": comp.name, "repository": comp.repository,
            "documentation": comp.documentation, "owner": comp.owner,
            "license": comp.license, "version": comp.version or row.get("version", ""),
            "maintenance": comp.maintenance, "self_hosted": comp.self_hosted,
            "paid": comp.paid, "paid_note": comp.paid_note,
            "api_requirements": comp.api_requirements,
            "cloud_requirements": comp.cloud_requirements,
            "installed": h.installed, "configured": h.configured,
            "state": h.state.value, "detail": h.detail,
        }

    def describe(self) -> Dict[str, Any]:
        return {
            "servers": self.c.section("capabilities").get("servers", []),
            "capabilities": {cid: {
                "name": c.name, "server": c.server, "tool": c.tool,
                "category": c.category, "description": c.description,
                "implementations": c.implementations, "fallback": c.fallback,
                "jit": c.jit, "permissions": [p.value for p in c.permissions],
                "platforms": c.platforms,
                "candidates": self.candidates(cid),
            } for cid, c in self.capabilities.items()},
            "components": {cid: self.provenance(cid) for cid in self.components},
        }


_REG: Optional[Registry] = None


def registry() -> Registry:
    global _REG
    if _REG is None:
        _REG = Registry()
    return _REG


def reset_registry() -> Registry:
    global _REG
    _REG = None
    return registry()
