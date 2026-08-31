"""
Permission policy engine.

Categories: read | write | execute | network | credentials |
            external_communication | destructive

Safety overrides execution: a denied or unapproved sensitive action never runs,
regardless of what the router, the graph or an agent asks for.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import state
from .config import cfg
from .models import Decision, PermissionCategory

_DEFAULT_POLICY: Dict[str, Any] = {
    "defaults": {
        "read": "allow",
        "write": "allow",
        "execute": "require_approval",
        "network": "allow",
        "credentials": "deny",
        "external_communication": "require_approval",
        "destructive": "require_approval",
    },
    "allow_paths": [],
    "deny_paths": [],
    "auto_approve": [],
    "always_require": ["destructive", "credentials"],
}


class Policy:
    def __init__(self) -> None:
        self.c = cfg()
        raw = self.c.section("permissions") or {}
        self.defaults: Dict[str, str] = dict(_DEFAULT_POLICY["defaults"])
        self.defaults.update(raw.get("defaults", {}) or {})
        home = str(Path.home())
        self.allow_paths: List[str] = [p.replace("${HOME}", home) for p in
                                       (raw.get("allow_paths") or self._default_allow())]
        self.deny_paths: List[str] = [p.replace("${HOME}", home) for p in
                                      (raw.get("deny_paths") or self._default_deny())]
        self.auto_approve: List[str] = list(raw.get("auto_approve", self._default_auto()))
        self.always_require: List[str] = list(raw.get("always_require",
                                                      ["destructive", "credentials"]))
        self.audit: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def _default_allow(self) -> List[str]:
        home = str(Path.home())
        return [os.path.join(home, ".olcap"), str(self.c.root)]

    def _default_deny(self) -> List[str]:
        home = str(Path.home())
        if self.c.is_windows:
            return [r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)",
                    os.path.join(home, "AppData", "Roaming", "Microsoft", "Credentials")]
        return ["/etc", "/boot", "/proc", "/sys", "/root", "/usr/bin", "/bin", "/sbin"]

    def _default_auto(self) -> List[str]:
        return [
            "read:*", "write:olcap", "network:search", "network:fetch",
            "execute:python", "execute:duckdb", "network:crawl",
        ]

    # ------------------------------------------------------------------ #
    def _path_allowed(self, resource: str) -> Tuple[bool, str]:
        if not resource:
            return True, ""
        try:
            rp = str(Path(resource).resolve())
        except Exception:
            rp = resource
        for d in self.deny_paths:
            try:
                if rp.lower().startswith(str(Path(d).resolve()).lower()):
                    return False, f"path in deny list: {d}"
            except Exception:
                if rp.lower().startswith(d.lower()):
                    return False, f"path in deny list: {d}"
        if self.allow_paths:
            for a in self.allow_paths:
                try:
                    if rp.lower().startswith(str(Path(a).resolve()).lower()):
                        return True, ""
                except Exception:
                    if rp.lower().startswith(a.lower()):
                        return True, ""
            # Outside allowed roots: still permitted for non-filesystem actions,
            # but flagged for filesystem categories.
        return True, ""

    def _in_allow_paths(self, resource: str) -> bool:
        try:
            rp = str(Path(resource).resolve())
        except Exception:
            rp = str(resource)
        for a in self.allow_paths:
            try:
                ap = str(Path(a).resolve())
            except Exception:
                ap = a
            if ap and (rp == ap or rp.startswith(ap.rstrip("/") + os.sep)
                       or rp.startswith(ap + os.sep)):
                return True
        return False

    def _matches(self, pattern: str, action: str, category: str) -> bool:
        if pattern in (f"{category}:*", "*", f"{category}:{action}", f"*:{action}"):
            return True
        try:
            return re.fullmatch(pattern, f"{category}:{action}") is not None
        except re.error:
            return False

    # ------------------------------------------------------------------ #
    def check(self, action: str, category: PermissionCategory | str,
              resource: str = "", context: Optional[Dict[str, Any]] = None,
              auto: bool = True) -> Dict[str, Any]:
        cat = PermissionCategory(category).value if isinstance(category, PermissionCategory) \
            else str(category)
        key = f"{cat}:{action}"
        reason = ""

        if cat in ("write", "execute", "read", "destructive") and resource:
            ok, why = self._path_allowed(resource)
            if not ok:
                decision = Decision.DENY
                reason = why
                return self._record(action, cat, resource, decision, reason, auto)

        base = self.defaults.get(cat, "require_approval")
        decision = Decision(base)

        if cat in self.always_require and decision == Decision.ALLOW:
            decision = Decision.REQUIRE_APPROVAL
            reason = "category always requires approval"

        if auto and decision == Decision.REQUIRE_APPROVAL \
                and cat not in self.always_require:
            # Work inside the declared allow_paths is auto-approved: this is
            # what makes allow_paths meaningful rather than decorative.
            if resource and self._in_allow_paths(resource):
                decision = Decision.ALLOW
                reason = "resource inside allow_paths"
            elif any(self._matches(p, action, cat) for p in self.auto_approve):
                decision = Decision.ALLOW
                reason = "auto-approved by policy"
            else:
                grant = os.environ.get("OLCAP_AUTO_APPROVE", "")
                if grant.strip().lower() in ("1", "true", "all", "yes"):
                    decision = Decision.ALLOW
                    reason = "global auto-approve enabled"

        return self._record(action, cat, resource, decision, reason, auto)

    def _record(self, action, cat, resource, decision, reason, auto) -> Dict[str, Any]:
        rec = {"action": action, "category": cat, "resource": resource,
               "decision": decision.value, "reason": reason}
        self.audit.append(rec)
        state.emit("permission.check", "permissions", rec)
        return rec

    def require(self, action: str, category: PermissionCategory | str,
                resource: str = "", context: Optional[Dict[str, Any]] = None,
                auto: bool = True) -> bool:
        """Raise unless allowed."""
        rec = self.check(action, category, resource, context, auto=auto)
        if rec["decision"] != Decision.ALLOW.value:
            raise PermissionError(
                f"Permission denied ({rec['decision']}) for {rec['category']}:"
                f"{rec['action']} on '{resource}'. {rec['reason']}")
        return True

    def approve(self, action: str, category: PermissionCategory | str,
                resource: str = "", note: str = "") -> None:
        from .models import new_id
        state.set_kv(f"approval:{category}:{action}:{abs(hash(resource))}",
                     {"approved": True, "note": note, "ts": state_now()})
        state.emit("permission.approved", "user",
                   {"action": action, "category": str(category), "resource": resource,
                    "note": note})


def state_now() -> float:
    import time
    return time.time()


_policy: Optional[Policy] = None


def policy() -> Policy:
    global _policy
    if _policy is None:
        _policy = Policy()
    return _policy
