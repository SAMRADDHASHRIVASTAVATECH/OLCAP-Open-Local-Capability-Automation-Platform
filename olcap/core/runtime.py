"""
Capability runtime: binds a capability to an implementation, executes it,
observes reality, and falls back / recovers without user intervention.

    REQUEST -> REGISTRY LOOKUP -> DEPENDENCY RESOLUTION -> ROUTER CHOICE ->
    (JIT ACTIVATION) -> EXECUTE -> OBSERVE -> VERIFY -> RECORD -> RETURN
                                     |
                                  failure -> next candidate -> repair -> retry
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import state
from .config import cfg
from .models import ComponentSpec, PermissionCategory
from .observability import span
from .permissions import policy
from .registry import registry
from .router import router

IMPLS: Dict[Tuple[str, str], Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def implements(capability: str, component: str):
    def deco(fn):
        IMPLS[(capability.upper(), component)] = fn
        return fn
    return deco


# --------------------------------------------------------------------------- #
# Run deadlines. A run publishes its wall-clock deadline here so that a single
# capability call can never outlive the objective it belongs to; the JIT
# timeout is capped by the time that is actually left.
# --------------------------------------------------------------------------- #
_deadline_lock = threading.Lock()
_run_deadline: Optional[float] = None


def set_deadline(deadline: Optional[float]) -> None:
    """Publish a run deadline (epoch seconds). The earliest one wins."""
    global _run_deadline
    with _deadline_lock:
        if deadline is None:
            _run_deadline = None
        elif _run_deadline is None or deadline < _run_deadline:
            _run_deadline = deadline


def clear_deadline(deadline: Optional[float] = None) -> None:
    global _run_deadline
    with _deadline_lock:
        if deadline is None or _run_deadline == deadline:
            _run_deadline = None


def jit_timeout(deadline: Optional[float] = None,
                cap: float = 180.0, floor: float = 10.0) -> float:
    """Timeout for one capability call, never longer than the run has left."""
    limit = deadline
    if limit is None:
        with _deadline_lock:
            limit = _run_deadline
    if not limit:
        return cap
    return max(floor, min(cap, limit - time.time()))

class CapabilityError(RuntimeError):
    def __init__(self, message: str, attempts: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.attempts = attempts or []


class Runtime:
    def __init__(self) -> None:
        self.c = cfg()
        self.reg = registry()
        self.rt = router()
        from .jit import jit
        self.jit = jit()
        # Bind every @implements() implementation. Deferred to here (rather
        # than module import time) because the implementation modules import
        # this module for the decorator.
        try:
            from .impls import register_all
            register_all()
        except Exception as e:      # pragma: no cover - defensive
            state.emit("runtime.impl_import_failed", "runtime",
                       {"error": f"{type(e).__name__}: {e}"})

    # ------------------------------------------------------------------ #
    def _permission_gate(self, capability_id: str, params: Dict[str, Any]) -> None:
        """
        Gate on the category the ACTION actually needs - not on every category
        the capability is allowed to use. Listing a directory must never require
        destructive approval just because the filesystem capability can delete.
        """
        cap = self.reg.capability(capability_id)
        if not cap:
            return
        action = str(params.get("action") or "").lower()
        needed = self._required_categories(cap, action, params)
        resource = str(params.get("path") or params.get("url") or
                       params.get("destination") or params.get("command") or "")
        for p in needed:
            rec = policy().check(action or capability_id.lower(), p, resource)
            if rec["decision"] != "allow":
                state.emit("capability.blocked", "permissions",
                           {"capability": capability_id, "action": action, **rec})
                raise PermissionError(
                    f"capability {capability_id} blocked by permission policy: "
                    f"{rec['decision']} on {p.value} for action '{action or 'default'}'")

    # ------------------------------------------------------------------ #
    _DESTRUCTIVE_VERBS = {"delete", "rm", "remove", "kill", "move", "rename",
                          "stop", "terminate", "drop", "format", "overwrite",
                          "forget", "purge", "close"}
    _WRITE_VERBS = {"write", "put", "save", "copy", "cp", "mkdir", "append",
                    "update", "set", "create", "add", "index", "ingest",
                    "schedule", "enqueue", "define", "upsert", "train"}
    _EXECUTE_VERBS = {"run", "execute", "start", "terminal", "shell", "navigate",
                      "click", "type", "key", "open", "focus", "resize",
                      "scroll", "press", "wait", "restart", "resume", "capture",
                      "screenshot"}
    _READ_VERBS = {"read", "list", "ls", "cat", "get", "stat", "info", "search",
                   "query", "profile", "describe", "head", "sample", "exists",
                   "show", "status", "fetch", "recall", "compare", "discover",
                   "verify", "report", "observe", "analyze", "analyse", "extract",
                   "browse", "crawl", "interact"}

    @classmethod
    def _required_categories(cls, cap, action: str, params: Dict[str, Any]):
        from .models import PermissionCategory as PC
        declared = set(cap.permissions)
        wanted: set = set()
        if action in cls._DESTRUCTIVE_VERBS:
            wanted.add(PC.DESTRUCTIVE)
        elif action in cls._WRITE_VERBS:
            wanted.add(PC.WRITE)
        elif action in cls._EXECUTE_VERBS:
            wanted.add(PC.EXECUTE)
        elif action in cls._READ_VERBS:
            wanted.add(PC.READ)
        else:
            wanted.add(PC.READ)
        # network is implied whenever a remote target is present
        if params.get("url") or params.get("urls") or action in ("browse", "crawl",
                                                                 "extract", "search"):
            wanted.add(PC.NETWORK)
        # keep only categories the capability itself declares (plus read)
        wanted = {w for w in wanted if w in declared} or {PC.READ}
        return sorted(wanted, key=lambda x: x.value)

    # ------------------------------------------------------------------ #
    def _ordered_components(self, capability_id: str, ctx: Dict[str, Any],
                            exclude: List[str]) -> List[ComponentSpec]:
        cap = self.reg.capability(capability_id)
        if not cap:
            return []
        comps: List[ComponentSpec] = []
        for cid in cap.implementations + cap.fallback:
            if cid in exclude:
                continue
            comp = self.reg.component(cid)
            if not comp:
                continue
            h = self.reg.health_of(cid)
            if not comp.enabled or not comp.supports(self.c.platform):
                continue
            if not h.installed or h.state.value in ("failed", "unavailable"):
                continue
            comps.append(comp)
        if not comps:
            return []
        chosen, reason = self.rt.choose(capability_id, comps, ctx)
        state.emit("router.decision", "router",
                   {"capability": capability_id, "chosen": chosen.id if chosen else None,
                    "reason": reason})
        ordered = ([chosen] if chosen else []) + \
                  [c for c in comps if not chosen or c.id != chosen.id]
        return ordered

    # ------------------------------------------------------------------ #
    def _run_one(self, comp: ComponentSpec, capability_id: str,
                 params: Dict[str, Any], method: str) -> Dict[str, Any]:
        from .worker import LOADERS
        if comp.id in LOADERS:
            out = self.jit.invoke(comp, method, params,
                                  timeout=jit_timeout())
            if not out.get("ok"):
                raise RuntimeError(out.get("error", "worker error"))
            return out.get("result", {})
        fn = IMPLS.get((capability_id.upper(), comp.id))
        if fn is None:
            raise NotImplementedError(
                f"no implementation bound for ({capability_id}, {comp.id})")
        out = fn(params)
        # An implementation that reports failure must be treated as a failure
        # so the next candidate gets its turn - otherwise a backend that
        # returns ok=False with no data silently wins.
        if isinstance(out, dict) and out.get("ok") is False:
            # Distinguish "this backend cannot do it" (no payload at all -> let
            # the next candidate try) from "operation ran and returned a
            # negative answer" (has a status/verdict -> that IS the answer).
            informative = any(k in out for k in
                              ("status", "passed", "rows", "columns", "items",
                               "results", "hits", "count", "report", "answer"))
            if not informative:
                raise RuntimeError(out.get("error") or out.get("note") or
                                   f"{comp.id} reported failure")
        return out

    # ------------------------------------------------------------------ #
    def execute(self, capability_id: str, params: Optional[Dict[str, Any]] = None,
                ctx: Optional[Dict[str, Any]] = None, method: Optional[str] = None,
                objective_id: str = "", node_id: str = "") -> Dict[str, Any]:
        cap_id = capability_id.upper()
        params = params or {}
        ctx = dict(ctx or {})
        # Routing context derived from the request itself: the router needs to
        # know it is dealing with a PDF vs a DOCX before it can pick an engine.
        _p = str(params.get("path") or params.get("url") or "")
        if "ext" not in ctx and "." in _p.rsplit("/", 1)[-1]:
            ctx["ext"] = "." + _p.rsplit(".", 1)[-1].lower()
        for k in ("pages", "depth", "max_results", "vectors", "corpus_docs",
                  "js_heavy", "tables", "offline"):
            if k not in ctx and params.get(k) is not None:
                # only scalars: a list of records is data, not routing context
                v = params[k]
                if isinstance(v, (bool, int, float, str)):
                    ctx[k] = v
                elif isinstance(v, (list, tuple, dict)):
                    ctx[k] = len(v)
        cap = self.reg.capability(cap_id)
        method = method or (cap.tool if cap else cap_id.lower())

        self._permission_gate(cap_id, params)

        attempts: List[Dict[str, Any]] = []
        exclude: List[str] = []
        started = time.time()

        for round_no in range(1, 5):     # replan-and-substitute rounds
            try:
                comps = self._ordered_components(cap_id, ctx, exclude)
            except Exception as e:
                # A routing failure must degrade to a capability failure with
                # attempts, never to an opaque exception.
                attempts.append({"component": "router",
                                 "error": f"{type(e).__name__}: {e}",
                                 "round": round_no})
                state.emit("router.error", "router",
                           {"capability": cap_id, "error": str(e)[:200]})
                comps = []
            if not comps:
                break
            for comp in comps:
                t0 = time.time()
                try:
                    with span("capability.execute", "capability",
                              {"capability": cap_id, "component": comp.id}) as sp:
                        result = self._run_one(comp, cap_id, params, method)
                        sp.set(component=comp.id, ok=True)
                    dur = (time.time() - t0) * 1000
                    self.rt.record(cap_id, ctx, comp, True, dur)
                    verified = None
                    if cap and cap.verification_required:
                        from .verification import verify_capability_output
                        verified = verify_capability_output(cap_id, result, params)
                    state.emit("capability.ok", "runtime",
                               {"capability": cap_id, "component": comp.id,
                                "ms": round(dur, 1), "objective_id": objective_id})
                    return {"ok": True, "capability": cap_id, "result": result,
                            "component": comp.id,
                            "provenance": self.reg.provenance(comp.id),
                            "duration_ms": round(dur, 1),
                            "verification": verified, "attempts": attempts}
                except Exception as e:
                    dur = (time.time() - t0) * 1000
                    self.rt.record(cap_id, ctx, comp, False, dur)
                    attempts.append({"component": comp.id,
                                     "error": f"{type(e).__name__}: {e}",
                                     "traceback": traceback.format_exc()[-600:],
                                     "round": round_no})
                    state.emit("capability.failed", "runtime",
                               {"capability": cap_id, "component": comp.id,
                                "error": str(e)[:300], "objective_id": objective_id})
                    exclude.append(comp.id)
                    # Try a repair once, then a substitute.
                    if round_no == 1:
                        try:
                            from ..manager.component_manager import repair
                            rep = repair(comp.id)
                            if rep.get("repaired") and \
                                    self.reg.health_of(comp.id).state.value == "healthy":
                                exclude.remove(comp.id)
                                attempts[-1]["repair"] = rep
                                # retry the same component immediately
                                try:
                                    result = self._run_one(comp, cap_id, params, method)
                                    return {"ok": True, "capability": cap_id,
                                            "result": result, "component": comp.id,
                                            "provenance": self.reg.provenance(comp.id),
                                            "duration_ms": round(dur, 1),
                                            "repair": rep, "attempts": attempts}
                                except Exception as e2:
                                    attempts.append({"component": comp.id,
                                                     "error": f"after repair: {e2}",
                                                     "round": round_no})
                                    exclude.append(comp.id)
                        except Exception as re_:
                            attempts[-1]["repair_error"] = str(re_)[:200]

        raise CapabilityError(
            f"capability {cap_id} failed on all implementations "
            f"({', '.join(a['component'] for a in attempts) or 'none available'})",
            attempts)


_RT: Optional[Runtime] = None


def runtime() -> Runtime:
    global _RT
    if _RT is None:
        _RT = Runtime()
    return _RT


def execute(capability_id: str, params: Optional[Dict[str, Any]] = None,
            ctx: Optional[Dict[str, Any]] = None, **kw) -> Dict[str, Any]:
    return runtime().execute(capability_id, params, ctx, **kw)
