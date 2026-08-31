"""
Failure recovery.

  CAPTURE -> CLASSIFY -> DIAGNOSE -> REPAIR OR SUBSTITUTE ->
  UPDATE GRAPH -> RETRY -> VERIFY

One implementation failing never terminates the objective if a valid
alternative exists.
"""
from __future__ import annotations

import re
import traceback
from typing import Any, Dict, List, Optional, Tuple

from . import state
from .models import ComponentSpec, NodeStatus
from .observability import span

CLASSIFIERS: List[Tuple[str, str]] = [
    (r"(?i)permission|denied|not permitted|access is denied", "permission"),
    (r"(?i)no module named|importerror|modulenotfound", "missing_dependency"),
    (r"(?i)worker .*(died|crash|exit|terminated)|subprocess .*(died|crash|exit)"
     r"|broken pipe|brokenpipe|no such process", "worker_crash"),
    (r"(?i)out of memory|memoryerror|cannot allocate|database is locked"
     r"|too many open files|no space left|resource temporarily unavailable",
     "resource"),
    (r"(?i)connection|timeout|timed out|temporary failure|dns", "transient_network"),
    (r"(?i)rate limit|429|too many requests", "rate_limited"),
    (r"(?i)not installed|executable .* missing|no such file", "not_installed"),
    (r"(?i)blocked by policy|403|forbidden", "blocked"),
    (r"(?i)invalid|validation|schema|bad request|400", "invalid_input"),

    (r"(?i)no .* returned results|empty", "empty_result"),
]


def classify(error: str) -> str:
    for pat, cat in CLASSIFIERS:
        if re.search(pat, error or ""):
            return cat
    return "unknown"


STRATEGY: Dict[str, List[str]] = {
    "permission": ["request_approval", "narrow_scope", "skip"],
    "missing_dependency": ["install_component", "substitute_component", "skip"],
    "transient_network": ["retry_backoff", "substitute_component", "substitute_source"],
    "rate_limited": ["retry_backoff", "substitute_component"],
    "not_installed": ["install_component", "substitute_component", "degrade"],
    "blocked": ["substitute_source", "substitute_component", "skip"],
    "invalid_input": ["normalize_input", "skip"],
    "resource": ["release_workers", "reduce_scope", "substitute_component"],
    "empty_result": ["substitute_source", "widen_query", "substitute_component"],
    "worker_crash": ["restart_worker", "substitute_component", "retry_backoff"],
    "unknown": ["retry_backoff", "substitute_component", "skip"],
}


def diagnose(error: str, component: Optional[str] = None) -> Dict[str, Any]:
    cat = classify(error)
    return {"category": cat, "component": component,
            "strategies": STRATEGY.get(cat, []),
            "error": (error or "")[:400],
            "hint": {
                "permission": "operation blocked by the permission policy",
                "missing_dependency": "a python module or executable is absent",
                "transient_network": "network flake; retrying usually works",
                "rate_limited": "upstream throttled the request",
                "not_installed": "backend not installed on this platform",
                "blocked": "target refused the request",
                "invalid_input": "input did not match the schema",
                "resource": "memory/CPU pressure",
                "empty_result": "backend returned nothing useful",
                "unknown": "unclassified failure",
            }.get(cat, "")}


def repair_component(component: str) -> Dict[str, Any]:
    """Ask the Component Manager to repair/reinstall a backend."""
    try:
        from ..manager.component_manager import repair
        return repair(component)
    except Exception as e:
        return {"repaired": False, "error": f"{type(e).__name__}: {e}"}


def recover(node_id: str, error: str, component: Optional[str] = None,
            graph: Any = None, attempt: int = 1) -> Dict[str, Any]:
    """Apply the recovery policy for a failed node and report what happened."""
    diag = diagnose(error, component)
    with span("recovery.recover", "recovery",
              {"node": node_id, "category": diag["category"]}) as sp:
        action = "none"
        detail = ""
        strategies = diag["strategies"]

        if component and "install_component" in strategies:
            rep = repair_component(component)
            if rep.get("repaired"):
                action, detail = "repaired_component", str(rep)[:200]
        if action == "none" and "retry_backoff" in strategies and attempt < 3:
            action, detail = "retry", f"attempt {attempt} -> backoff and retry"
        if action == "none" and "substitute_component" in strategies and graph:
            sub = graph.substitute(node_id, reason=f"{diag['category']}: {error[:120]}")
            if sub:
                action, detail = "substituted", f"new node {sub.id}"
        if action == "none" and "release_workers" in strategies:
            try:
                from .jit import jit
                jit().release_all()
                action, detail = "released_workers", "worker pools released"
            except Exception:
                pass
        if action == "none":
            action, detail = "skipped", "no strategy available; node marked failed"

        out = {"node": node_id, "diagnosis": diag, "action": action, "detail": detail}
        sp.set(action=action, category=diag["category"])
        state.emit("recovery.applied", "recovery", out)
        return out


def failure_report(objective_id: Optional[str] = None) -> Dict[str, Any]:
    evs = state.events(500, kind="capability.failed", objective_id=objective_id)
    by_cat: Dict[str, int] = {}
    for e in evs:
        cat = classify(str(e["payload"].get("error", "")))
        by_cat[cat] = by_cat.get(cat, 0) + 1
    return {"failures": len(evs), "by_category": by_cat,
            "recent": evs[:20]}
