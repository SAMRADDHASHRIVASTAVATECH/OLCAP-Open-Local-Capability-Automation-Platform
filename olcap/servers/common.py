
import json
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from ..core import state
from ..core.config import cfg
from ..core.health import check_all, summary as health_summary
from ..core.jit import jit as _jit
from ..core.observability import span
from ..core.registry import registry
from ..core.runtime import CapabilityError
from ..core.verification import verify_artifact

PY_TYPES = {"string": "str", "integer": "int", "number": "float",
            "boolean": "bool", "array": "list", "object": "dict"}
PY_DEFAULTS = {"string": '""', "integer": "0", "number": "0.0",
               "boolean": "False", "array": "None", "object": "None"}


def _default_literal(meta: Dict[str, Any]) -> str:
    """Render the declared default as a Python literal - the schema default
    must be the real default, otherwise e.g. limit=0 silently truncates."""
    t = str(meta.get("type", "string"))
    if "default" in meta:
        d = meta["default"]
        if isinstance(d, bool):
            return "True" if d else "False"
        if isinstance(d, (int, float)):
            return repr(d)
        if isinstance(d, str):
            return repr(d)
        return "None"
    return PY_DEFAULTS.get(t, "None")


def make_tool_fn(spec):
    """Generate a typed tool function from a capability's declared inputs."""
    args: List[str] = []
    for name, meta in (spec.inputs or {}).items():
        t = PY_TYPES.get(str(meta.get("type", "string")), "str")
        if meta.get("required"):
            args.append(f"{name}: {t}")
        else:
            args.append(f"{name}: {t} = {_default_literal(meta)}")

    # A catch-all dict lets callers pass keys the declared schema does not
    # enumerate (e.g. data_analyze(destination=...)) without the registry
    # having to know every key up front. Some capabilities already declare a
    # `params` object as their payload - in that case it IS the payload.
    has_params_arg = "params" in (spec.inputs or {})
    if not has_params_arg:
        args.append("params: dict = None")

    sig = ", ".join(args)
    body_args = ", ".join(
        f'"{n}": {n}' for n in (spec.inputs or {})
    ) or ""

    # Unset optional arguments are omitted entirely: an empty string is a
    # meaningful value in some backends and must not be sent by accident.
    if has_params_arg:
        src = f"""
def _tool({sig}):
    p = {{_k: _v for _k, _v in ({{{body_args}}}).items()
          if _v is not None and _v != ""}}
    p.update(params or {{}})
    return _invoke("{spec.id}", p)
"""
    else:
        src = f"""
def _tool({sig}):
    p = {{_k: _v for _k, _v in ({{{body_args}}}).items()
          if _v is not None and _v != ""}}
    if params:
        p.update(params)
    return _invoke("{spec.id}", p)
"""

    ns = {"_invoke": invoke}

    exec(
        compile(src, f"<tool:{spec.tool}>", "exec"),
        ns
    )  # noqa: S102

    # Because this module uses `from __future__ import annotations`, the
    # dynamically generated function receives annotation values such as
    # "str", "int", "list", and "dict" as strings. FastMCP expects actual
    # Python type objects when it inspects the function signature.
    fn = ns["_tool"]
    fn.__annotations__ = {
        key: eval(value) if isinstance(value, str) else value
        for key, value in fn.__annotations__.items()
    }

    return fn


def invoke(capability_id: str, params: Dict[str, Any]):
    """
    One capability invocation: permission gate, registry lookup, router choice,
    JIT activation, execution, verification, provenance and observability.
    """
    started = time.time()
    try:
        from ..core.runtime import runtime
        out = runtime().execute(capability_id, params or {})
        out["server_time_ms"] = round((time.time() - started) * 1000, 1)
        return out
    except CapabilityError as e:
        return {
            "ok": False,
            "capability": capability_id,
            "error": str(e)[:500],
            "attempts": e.attempts,
            "server_time_ms": round((time.time() - started) * 1000, 1),
        }
    except PermissionError as e:
        return {
            "ok": False,
            "capability": capability_id,
            "error": str(e),
            "blocked": True,
        }
    except Exception as e:
        return {
            "ok": False,
            "capability": capability_id,
            "error": f"{type(e).__name__}: {e}",
            "server_time_ms": round((time.time() - started) * 1000, 1),
        }


def build_server(
    server_id: str,
    name: str,
    description: str
) -> FastMCP:
    reg = registry()
    srv = FastMCP(name=name, instructions=description)

    # ------------------------------------------------------------------ #
    # Capability tools (generated from the registry)
    # ------------------------------------------------------------------ #
    registered: List[str] = []

    for spec in reg.capabilities_for_server(server_id):
        fn = make_tool_fn(spec)
        fn.__name__ = spec.tool
        fn.__doc__ = (
            f"[{spec.id}] {spec.description}\n\n"
            f"Category: {spec.category}. "
            f"Implementations: {', '.join(spec.implementations)}. "
            f"Fallback: {', '.join(spec.fallback) or 'none'}. "
            f"Permissions: {', '.join(p.value for p in spec.permissions)}."
            + (
                " Verification is required for this capability."
                if spec.verification_required
                else ""
            )
        )
        srv.tool(name=spec.tool)(fn)
        registered.append(spec.tool)

    # ------------------------------------------------------------------ #
    # Shared core tools (identical on all three servers)
    # ------------------------------------------------------------------ #

    @srv.tool(
        name="core_capabilities",
        description=(
            "List every capability exposed by this server and all "
            "three servers, with implementations, fallbacks, "
            "installation and health state."
        ),
    )
    def core_capabilities(scope: str = "server"):
        if scope == "all":
            return {"ok": True, "registry": reg.describe()}

        caps = reg.capabilities_for_server(server_id)

        return {
            "ok": True,
            "server": server_id,
            "capabilities": [
                {
                    "id": c.id,
                    "tool": c.tool,
                    "name": c.name,
                    "category": c.category,
                    "description": c.description,
                    "implementations": c.implementations,
                    "fallback": c.fallback,
                    "permissions": [p.value for p in c.permissions],
                    "jit": c.jit,
                    "platforms": c.platforms,
                    "candidates": reg.candidates(c.id),
                }
                for c in caps
            ],
        }

    @srv.tool(
        name="core_health",
        description=(
            "Health of this server, every component and the JIT "
            "worker pools."
        ),
    )
    def core_health(scope: str = "server"):
        if scope in ("all", "components"):
            data = check_all()
        else:
            data = {
                cid: reg.health_of(cid).model_dump()
                for cid in {
                    c
                    for cap in reg.capabilities_for_server(server_id)
                    for c in cap.implementations + cap.fallback
                }
            }

        return {
            "ok": True,
            "server": server_id,
            "platform": cfg().platform,
            "components": {
                k: (v if isinstance(v, dict) else v.model_dump())
                for k, v in data.items()
            },
            "jit": _jit().pool_state(),
            "summary": health_summary(),
        }

    @srv.tool(
        name="core_objective",
        description=(
            "Unified Core: set a high-level objective, plan the "
            "dependency graph, execute it end to end, and report "
            "status. The system selects capabilities itself."
        ),
    )
    def core_objective(
        action: str = "status",
        objective: str = "",
        objective_id: str = "",
        success_criteria: str = "",
        parallel: bool = True,
        max_iterations: int = 40,
        use_model: bool = True,
    ):
        from ..core.orchestrator import UnifiedCore

        action = (action or "status").lower()

        if action == "set":
            c = UnifiedCore()
            crit = None

            if success_criteria:
                try:
                    crit = json.loads(success_criteria)
                except Exception:
                    crit = [
                        c.strip()
                        for c in success_criteria.split(";")
                        if c.strip()
                    ]

            out = c.plan(objective, crit, use_model=use_model)
            out["objective_id"] = c.objective_id

            return {"ok": True, **out}

        if action == "run":
            c = UnifiedCore(objective_id or None)
            crit = None

            if success_criteria:
                try:
                    crit = json.loads(success_criteria)
                except Exception:
                    crit = [
                        c.strip()
                        for c in success_criteria.split(";")
                        if c.strip()
                    ]

            return {
                "ok": True,
                **(
                    c.run(
                        objective or None,
                        crit,
                        max_iterations=max_iterations,
                        parallel=parallel,
                        use_model=use_model,
                    )
                ),
            }

        if action == "status":
            c = UnifiedCore(
                objective_id or UnifiedCore.current() or "none"
            )
            return {"ok": True, **(c.status())}

        return {
            "ok": False,
            "error": f"unknown action {action}",
        }

    @srv.tool(
        name="core_graph",
        description=(
            "Inspect or mutate the live dependency graph: nodes, "
            "edges, readiness, blocking, critical path, cycles."
        ),
    )
    def core_graph(
        objective_id: str = "",
        action: str = "snapshot",
    ):
        from ..core.graph import DependencyGraph

        oid = objective_id or UnifiedCore_current() or ""

        if not oid:
            return {
                "ok": False,
                "error": "objective_id required",
            }

        g = DependencyGraph(oid)
        action = (action or "snapshot").lower()

        if action == "snapshot":
            return {"ok": True, **g.snapshot()}

        if action == "ready":
            return {
                "ok": True,
                "ready": [n.model_dump() for n in g.ready()],
            }

        if action == "cycles":
            return {
                "ok": True,
                "cycles": g.find_cycles(),
            }

        if action == "critical_path":
            return {
                "ok": True,
                "critical_path": g.critical_path(),
            }

        if action == "progress":
            return {
                "ok": True,
                **g.progress(),
            }

        return {
            "ok": False,
            "error": f"unknown action {action}",
        }

    @srv.tool(
        name="core_verify",
        description=(
            "Verify a result or artifact against explicit success "
            "criteria."
        ),
    )
    def core_verify(
        criteria: str = "[]",
        result: str = "{}",
        artifact_id: str = "",
    ):
        try:
            crit = json.loads(criteria) if criteria else []
        except Exception:
            crit = [
                c.strip()
                for c in criteria.split(";")
                if c.strip()
            ]

        try:
            res = json.loads(result) if result else {}
        except Exception:
            res = {"raw": result}

        return verify_artifact(
            {
                "criteria": crit,
                "result": res,
                "artifact_id": artifact_id,
            }
        )

    @srv.tool(
        name="core_component",
        description=(
            "Component Manager: verify provenance, install, "
            "configure, start, stop, restart, update, remove, "
            "enable/disable, validate, repair, roll back, "
            "health-check a backend. Idempotent."
        ),
    )
    def core_component(
        action: str = "list",
        component: str = "",
        force: bool = False,
        include_optional: str = "",
    ):
        from ..manager.component_manager import manager

        m = manager()
        action = (action or "list").lower()
        cid = component

        if action == "list":
            return {
                "ok": True,
                "components": m.report()["components"],
            }

        if action == "verify":
            return m.verify(cid)

        if action == "install":
            return m.install(cid, force=force)

        if action == "install_all":
            opts = [
                x for x in include_optional.split(",")
                if x
            ]
            return m.install_all(
                only_required=True,
                include_optional=opts,
            )

        if action == "remove":
            return m.remove(cid)

        if action == "repair":
            return m.repair(cid)

        if action == "rollback":
            return m.rollback(cid)

        if action == "update":
            return m.update(cid)

        if action == "start":
            return m.start(cid)

        if action == "stop":
            return m.stop(cid)

        if action == "restart":
            return m.restart(cid)

        if action == "validate":
            return m.validate(cid)

        if action == "health":
            return m.health_check(cid)

        if action in ("enable", "disable"):
            return m.enable(cid, action == "enable")

        if action == "report":
            return {
                "ok": True,
                **m.report(),
            }

        return {
            "ok": False,
            "error": f"unknown action {action}",
        }

    @srv.tool(
        name="core_observability",
        description=(
            "Spans, counters, events, health and JIT pool state."
        ),
    )
    def core_observability(
        scope: str = "summary",
        limit: int = 100,
    ):
        from ..core.observability import counters, recent_spans

        return {
            "ok": True,
            "counters": counters(),
            "spans": recent_spans(limit),
            "events": state.events(limit),
        }

    srv._olcap_registered = registered  # type: ignore[attr-defined]
    return srv


def UnifiedCore_current() -> str:
    from ..core.orchestrator import UnifiedCore
    return UnifiedCore.current() or ""


def run_server(
    server_id: str,
    name: str,
    description: str,
) -> None:
    srv = build_server(server_id, name, description)
    transport = (
        cfg().env("MCP_TRANSPORT") or "stdio"
    ).lower()
    srv.run(transport)
