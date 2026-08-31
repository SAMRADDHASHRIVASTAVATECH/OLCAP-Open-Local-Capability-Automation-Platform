"""
MCP SERVER 3 implementations - Automation: workflows and durable tasks.

Durable by construction: every run and every task is checkpointed into the
authoritative SQLite state, so a crash, a restart or a cancelled process can
resume exactly where it stopped. Task state is never lost.
"""
from __future__ import annotations

import json
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional

from .. import state
from ..observability import span
from ..runtime import implements

# Step kinds a workflow node can execute.
#   capability -> invoke an OLCAP capability (any of the three MCP servers)
#   task       -> invoke a registered python callable
#   workflow   -> nested workflow
_STEP_HANDLERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Any]] = {}


def register_step_handler(name: str, fn: Callable[[Dict[str, Any], Dict[str, Any]], Any]) -> None:
    _STEP_HANDLERS[name] = fn


def _now() -> float:
    return time.time()


def _wf_key(run_id: str) -> str:
    return f"workflow.run.{run_id}"


def _task_key(tid: str) -> str:
    return f"task.{tid}"


# --------------------------------------------------------------------------- #
# WORKFLOW_EXECUTION
# --------------------------------------------------------------------------- #
def _run_step(step: Dict[str, Any], ctx: Dict[str, Any]) -> Any:
    kind = step.get("do", "capability")
    if kind == "capability":
        from ..runtime import execute
        params = _interp(step.get("params") or {}, ctx)
        out = execute(step["capability"], params, method=step.get("method"))
        return out
    if kind == "task":
        fn = _STEP_HANDLERS.get(step.get("name") or "")
        if fn is None:
            raise ValueError(f"unknown task handler: {step.get('name')}")
        return fn(_interp(step.get("params") or {}, ctx), ctx)
    if kind == "python":
        safe = {"ctx": ctx, "json": json, "sum": sum, "len": len, "str": str}
        return eval(step["expr"], {"__builtins__": {}}, safe)  # noqa: S307
    raise ValueError(f"unknown step kind: {kind}")


def _interp(obj: Any, ctx: Dict[str, Any]) -> Any:
    """Replace {{steps.<id>.<field>}} / {{ctx.<k>}} placeholders."""
    if isinstance(obj, str):
        def rep(m):
            path = m.group(1).split(".")
            cur: Any = {"ctx": ctx, "steps": ctx.get("_steps", {}), **ctx}
            for p in path:
                if isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    return m.group(0)
            return cur if isinstance(cur, (str, int, float)) else json.dumps(cur)
        return re_place(obj, rep)
    if isinstance(obj, dict):
        return {k: _interp(v, ctx) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interp(v, ctx) for v in obj]
    return obj


import re as _re


def re_place(s: str, rep) -> str:
    return _re.sub(r"\{\{([a-zA-Z0-9_\.]+)\}\}", rep, s)


def _topo(steps: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group steps into dependency levels so independent steps run together."""
    by_id = {s["id"]: s for s in steps}
    deps = {s["id"]: list(s.get("depends_on") or []) for s in steps}
    levels: List[List[Dict[str, Any]]] = []
    done = set()
    while len(done) < len(steps):
        level = [s for s in steps if s["id"] not in done
                 and all(d in done for d in deps[s["id"]])]
        if not level:      # cycle: emit remaining at once rather than deadlock
            level = [s for s in steps if s["id"] not in done]
        for s in level:
            done.add(s["id"])
        levels.append(level)
    return levels


def _execute_workflow(run: Dict[str, Any], parallel: bool = False) -> Dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor
    ctx: Dict[str, Any] = dict(run.get("context") or {})
    steps_out: Dict[str, Any] = {}
    run["state"] = "running"
    run["updated_at"] = _now()
    state.set_kv(_wf_key(run["run_id"]), run)
    state.emit("workflow.started", "workflow", {"run_id": run["run_id"]})

    for level in _topo(run["steps"]):
        def _one(step):
            sid = step["id"]
            rec = {"id": sid, "status": "running", "started_at": _now()}
            steps_out[sid] = rec
            for attempt in range(1, int(step.get("retries", 1)) + 1):
                try:
                    with span("workflow.step", "workflow",
                              {"run_id": run["run_id"], "step": sid}) as sp:
                        out = _run_step(step, {**ctx, "_steps": steps_out})
                        sp.set(ok=True)
                    rec.update(status="succeeded", finished_at=_now(),
                               output=_jsonable(out), attempts=attempt)
                    if step.get("save_as"):
                        ctx[step["save_as"]] = _jsonable(out)
                    break
                except Exception as e:
                    rec.update(status="failed", error=f"{type(e).__name__}: {e}",
                               attempts=attempt,
                               traceback=traceback.format_exc()[-500:])
                    if attempt >= int(step.get("retries", 1)):
                        break
                    time.sleep(min(2 ** attempt, 5))
            rec["finished_at"] = rec.get("finished_at") or _now()
            return rec

        if parallel and len(level) > 1:
            with ThreadPoolExecutor(max_workers=min(4, len(level))) as ex:
                list(ex.map(_one, level))
        else:
            for s in level:
                _one(s)

        if any(steps_out[s["id"]].get("status") == "failed" for s in level) \
                and run.get("fail_fast", True):
            break

    run["steps_out"] = steps_out
    run["context"] = ctx
    ok = all(v.get("status") == "succeeded" for v in steps_out.values())
    run["state"] = "succeeded" if ok else "failed"
    run["finished_at"] = _now()
    state.set_kv(_wf_key(run["run_id"]), run)
    state.emit("workflow.finished", "workflow",
               {"run_id": run["run_id"], "state": run["state"]})
    return run


def _jsonable(o: Any) -> Any:
    try:
        json.dumps(o)
        return o
    except Exception:
        return str(o)


@implements("WORKFLOW_EXECUTION", "olcap-workflows")
def workflow_run(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "run").lower()
    if action in ("define", "run", "start", "create"):
        wf = params.get("workflow") or {}
        if not wf.get("steps"):
            raise ValueError("workflow.steps is required")
        run_id = wf.get("run_id") or ("run_" + uuid.uuid4().hex[:12])
        run = {"run_id": run_id, "name": wf.get("name", "workflow"),
               "steps": wf["steps"], "context": wf.get("context") or {},
               "fail_fast": bool(wf.get("fail_fast", True)),
               "state": "pending", "created_at": _now()}
        state.set_kv(_wf_key(run_id), run)
        if action == "define":
            return {"ok": True, "run_id": run_id, "state": "defined",
                    "steps": len(run["steps"])}
        return {"ok": True, **{k: v for k, v in
                               _execute_workflow(run, parallel=bool(wf.get("parallel"))).items()
                               if k in ("run_id", "state", "steps_out", "context")}}

    if action == "status":
        run = state.get_kv(_wf_key(params["run_id"]))
        if not run:
            raise KeyError(f"unknown run {params['run_id']}")
        return {"ok": True, **run}

    if action in ("resume", "retry"):
        run = state.get_kv(_wf_key(params["run_id"]))
        if not run:
            raise KeyError(f"unknown run {params['run_id']}")
        # skip already-succeeded steps (durable resume)
        done = {k for k, v in (run.get("steps_out") or {}).items()
                if v.get("status") == "succeeded"}
        run["steps"] = [s for s in run["steps"]
                        if s["id"] not in done or
                        any(d not in done for d in s.get("depends_on") or [])]
        return {"ok": True, **{k: v for k, v in _execute_workflow(run).items()
                               if k in ("run_id", "state", "steps_out")}}

    if action == "cancel":
        run = state.get_kv(_wf_key(params["run_id"])) or {}
        run["state"] = "cancelled"
        run["finished_at"] = _now()
        state.set_kv(_wf_key(params["run_id"]), run)
        return {"ok": True, "run_id": params["run_id"], "state": "cancelled"}

    if action == "list":
        keys = [k for k in state.all_kv() if k.startswith("workflow.run.")]
        return {"ok": True, "runs": [state.get_kv(k) for k in keys]}

    raise ValueError(f"unknown workflow action: {action}")


# --------------------------------------------------------------------------- #
# DURABLE_TASKS
# --------------------------------------------------------------------------- #
@implements("DURABLE_TASKS", "olcap-workflows")
def task_schedule(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "enqueue").lower()
    if action in ("enqueue", "schedule", "submit"):
        task = params.get("task") or {}
        tid = task.get("task_id") or ("task_" + uuid.uuid4().hex[:12])
        rec = {"task_id": tid, "kind": task.get("kind", "capability"),
               "payload": task.get("payload") or task,
               "max_retries": int(params.get("max_retries",
                                             task.get("max_retries", 3))),
               "attempts": 0, "status": "queued", "created_at": _now(),
               "updated_at": _now(), "result": None, "error": None}
        state.set_kv(_task_key(tid), rec)
        state.emit("task.queued", "durable", {"task_id": tid})
        return {"ok": True, "task_id": tid, "status": "queued"}

    if action in ("run", "start", "execute"):
        tid = params.get("task_id")
        rec = state.get_kv(_task_key(tid))
        if not rec:
            raise KeyError(f"unknown task {tid}")
        rec["status"] = "running"
        rec["updated_at"] = _now()
        state.set_kv(_task_key(tid), rec)
        payload = rec["payload"]
        last_err = None
        for attempt in range(1, rec["max_retries"] + 1):
            rec["attempts"] = attempt
            try:
                with span("durable.task", "durable",
                          {"task_id": tid, "attempt": attempt}) as sp:
                    if rec["kind"] == "capability":
                        from ..runtime import execute
                        out = execute(payload.get("capability"),
                                      payload.get("params") or {},
                                      method=payload.get("method"))
                    else:
                        fn = _STEP_HANDLERS.get(rec["kind"])
                        if fn is None:
                            raise ValueError(f"unknown task kind {rec['kind']}")
                        out = fn(payload, {})
                    sp.set(ok=True)
                rec.update(status="succeeded", result=_jsonable(out),
                           finished_at=_now(), updated_at=_now(), error=None)
                state.set_kv(_task_key(tid), rec)
                state.emit("task.succeeded", "durable",
                           {"task_id": tid, "attempts": attempt})
                return {"ok": True, "task_id": tid, "status": "succeeded",
                        "attempts": attempt, "result": _jsonable(out)}
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                rec["error"] = last_err
                rec["status"] = "retrying"
                rec["updated_at"] = _now()
                state.set_kv(_task_key(tid), rec)   # checkpoint before sleeping
                state.emit("task.failed", "durable",
                           {"task_id": tid, "attempt": attempt, "error": last_err[:200]})
                time.sleep(min(2 ** attempt, 8))
        rec.update(status="failed", error=last_err, updated_at=_now())
        state.set_kv(_task_key(tid), rec)
        return {"ok": False, "task_id": tid, "status": "failed",
                "attempts": rec["attempts"], "error": last_err}

    if action == "status":
        rec = state.get_kv(_task_key(params["task_id"]))
        if not rec:
            raise KeyError(f"unknown task {params['task_id']}")
        return {"ok": True, **rec}

    if action == "list":
        keys = [k for k in state.all_kv() if k.startswith("task.")]
        return {"ok": True, "tasks": [state.get_kv(k) for k in keys]}

    if action in ("resume",):
        rec = state.get_kv(_task_key(params["task_id"])) or {}
        rec["status"] = "queued"
        state.set_kv(_task_key(rec["task_id"]), rec)
        return task_schedule({"action": "run", "task_id": rec["task_id"]})

    raise ValueError(f"unknown task action: {action}")
