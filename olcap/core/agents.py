"""
Agent system.

Agents are internal execution engines - none of them is the master controller.
They share one goal, one graph, one memory, one permission policy, one event
stream and one artifact store. External frameworks (LangGraph, CrewAI, AG2,
smolagents, OpenHands, PraisonAI, AgentScope) can be plugged in as engines via
the Component Manager, but the Unified Core stays authoritative.
"""
from __future__ import annotations

import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import state
from .models import AgentRole, GraphNode, NodeStatus, NodeType, new_id
from .observability import span

ROLE_PROFILE: Dict[str, Dict[str, Any]] = {
    AgentRole.PLANNER: {"description": "decomposes objectives and defines success criteria",
                        "capabilities": ["MEMORY", "RESEARCH"]},
    AgentRole.RESEARCHER: {"description": " gathers and verifies evidence",
                           "capabilities": ["RESEARCH", "WEB_SEARCH", "WEB_EXTRACT",
                                            "SOURCE_VERIFICATION", "MEMORY"]},
    AgentRole.BROWSER: {"description": "drives web pages and crawls",
                        "capabilities": ["WEB_BROWSE", "WEB_CRAWL", "WEB_INTERACT",
                                         "WEB_EXTRACT"]},
    AgentRole.CODER: {"description": "writes and modifies code",
                      "capabilities": ["FILESYSTEM", "TERMINAL", "DATABASE_QUERY"]},
    AgentRole.TESTER: {"description": "executes tests and validates outputs",
                       "capabilities": ["TERMINAL", "VERIFICATION", "FILESYSTEM"]},
    AgentRole.DEBUGGER: {"description": "diagnoses failures and proposes fixes",
                         "capabilities": ["TERMINAL", "FILESYSTEM", "MEMORY"]},
    AgentRole.DOCUMENT: {"description": "processes documents and builds knowledge",
                         "capabilities": ["DOCUMENT_INTELLIGENCE", "RAG",
                                          "KNOWLEDGE_SEARCH", "FILESYSTEM"]},
    AgentRole.DATA: {"description": "analyses data and queries databases",
                     "capabilities": ["DATA_ANALYSIS", "DATABASE_QUERY",
                                      "VECTOR_STORAGE"]},
    AgentRole.SECURITY: {"description": "reviews permissions, provenance and risk",
                         "capabilities": ["FILESYSTEM", "TERMINAL", "MEMORY"]},
    AgentRole.VERIFIER: {"description": "verifies results against success criteria",
                         "capabilities": ["VERIFICATION", "SOURCE_VERIFICATION"]},
    AgentRole.AUTOMATION: {"description": "runs workflows and durable tasks",
                           "capabilities": ["WORKFLOW_EXECUTION", "DURABLE_TASKS",
                                            "PROCESS_CONTROL", "WINDOWS_CONTROL"]},
}


@dataclass
class Agent:
    id: str
    role: AgentRole
    status: str = "idle"
    tasks_done: int = 0
    tasks_failed: int = 0
    created_at: float = field(default_factory=time.time)
    last_error: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)

    def profile(self) -> Dict[str, Any]:
        return ROLE_PROFILE.get(self.role, {})


class AgentSystem:
    def __init__(self, objective_id: str) -> None:
        self.objective_id = objective_id
        self.agents: Dict[str, Agent] = {}
        self.handoffs: List[Dict[str, Any]] = []
        self._ensure_core_roles()

    # ------------------------------------------------------------------ #
    def _ensure_core_roles(self) -> None:
        for role in (AgentRole.PLANNER, AgentRole.RESEARCHER, AgentRole.VERIFIER):
            self.spawn(role)

    def spawn(self, role: AgentRole | str) -> Agent:
        a = Agent(id=new_id("agent"), role=AgentRole(role))
        self.agents[a.id] = a
        state.emit("agent.spawned", "agents",
                   {"agent": a.id, "role": a.role.value}, self.objective_id)
        return a

    def get(self, aid: str) -> Optional[Agent]:
        return self.agents.get(aid)

    def by_role(self, role: AgentRole | str) -> List[Agent]:
        return [a for a in self.agents.values() if a.role.value == str(role)]

    def pick(self, capability: Optional[str] = None) -> Agent:
        """Choose a free agent whose role declares the required capability."""
        if capability:
            for a in self.agents.values():
                if a.status == "idle" and capability in (a.profile().get("capabilities") or []):
                    return a
        free = [a for a in self.agents.values() if a.status == "idle"]
        if free:
            return free[0]
        # bounded: spawn a new agent rather than queue forever
        return self.spawn(AgentRole.RESEARCHER if capability in
                          ("RESEARCH", "WEB_SEARCH") else AgentRole.AUTOMATION)

    def restart(self, aid: str) -> Agent:
        old = self.agents.get(aid)
        if not old:
            raise KeyError(aid)
        new = self.spawn(old.role)
        old.status = "restarted"
        state.emit("agent.restarted", "agents",
                   {"old": aid, "new": new.id, "role": old.role.value},
                   self.objective_id)
        return new

    def cancel(self, aid: str) -> Agent:
        a = self.agents.get(aid)
        if a:
            a.status = "cancelled"
            state.emit("agent.cancelled", "agents", {"agent": aid}, self.objective_id)
        return a

    # ------------------------------------------------------------------ #
    def handoff(self, src: str, dst: str, context: Dict[str, Any]) -> None:
        rec = {"from": src, "to": dst, "context_keys": sorted(context.keys()),
               "ts": time.time()}
        self.handoffs.append(rec)
        if dst in self.agents:
            self.agents[dst].context.update(context)
        state.emit("agent.handoff", "agents", rec, self.objective_id)

    # ------------------------------------------------------------------ #
    def run(self, agent: Agent, node: GraphNode,
            invoke: Callable[[str, Dict[str, Any], Dict[str, Any]], Dict[str, Any]]
            ) -> Dict[str, Any]:
        """Execute one task node through this agent."""
        agent.status = "running"
        t0 = time.time()
        try:
            with span("agent.task", "agent",
                      {"agent": agent.id, "role": agent.role.value,
                       "node": node.id, "capability": node.capability}) as sp:
                memory = self._recall(node)
                params = dict(node.payload or {})
                if node.capability:
                    out = invoke(node.capability, params,
                                 {"agent": agent.role.value, "memory": memory})
                else:
                    out = {"ok": True, "result": {"note": "no capability required",
                                                  "title": node.title}}
                dur = round((time.time() - t0) * 1000, 1)
                sp.set(ms=dur, ok=True)
                agent.status = "idle"
                agent.tasks_done += 1
                self._remember(node, out)
                state.emit("agent.task.ok", "agents",
                           {"agent": agent.id, "node": node.id, "ms": dur},
                           self.objective_id)
                return {"ok": True, "agent": agent.id, "output": out,
                        "duration_ms": dur}
        except Exception as e:
            agent.status = "idle"
            agent.tasks_failed += 1
            agent.last_error = f"{type(e).__name__}: {e}"
            state.emit("agent.task.failed", "agents",
                       {"agent": agent.id, "node": node.id,
                        "error": agent.last_error[:300]}, self.objective_id)
            return {"ok": False, "agent": agent.id,
                    "error": agent.last_error,
                    "traceback": traceback.format_exc()[-800:]}
        finally:
            if agent.status == "running":
                agent.status = "idle"

    # ------------------------------------------------------------------ #
    def _recall(self, node: GraphNode) -> List[Dict[str, Any]]:
        try:
            from .runtime import execute
            q = f"{node.title} {node.capability or ''}"
            out = execute("MEMORY", {"action": "search", "query": q, "limit": 3},
                          method="memory_op")
            return out["result"].get("items", [])
        except Exception:
            return []

    def _remember(self, node: GraphNode, out: Dict[str, Any]) -> None:
        try:
            from .runtime import execute
            summary = str(out.get("result", ""))[:600]
            execute("MEMORY", {"action": "put", "kind": "episodic",
                               "text": f"[{node.capability}] {node.title}: {summary}",
                               "meta": {"node": node.id,
                                        "objective": self.objective_id},
                               "salience": 0.6}, method="memory_op")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    def run_parallel(self, jobs: List[tuple], invoke: Callable, max_workers: int = 4,
                     timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        """jobs: list of (agent, node). Results are returned in SUBMISSION
        order - as_completed yields completion order, and mapping them by
        position would attribute every result to the wrong task.

        There is one result per job, always. Dropping unfinished jobs would
        make the caller zip results against the wrong nodes. A job that has
        not finished when the deadline hits gets an explicit timeout result
        and its thread is abandoned rather than waited on.
        """
        results: List[Optional[Dict[str, Any]]] = [None] * len(jobs)
        if not jobs:
            return []
        ex = ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(jobs))),
                                thread_name_prefix="olcap-agents")
        try:
            index = {ex.submit(self.run, a, n, invoke): i
                     for i, (a, n) in enumerate(jobs)}
            done, not_done = wait(list(index), timeout=timeout)
            for fut in done:
                i = index[fut]
                a, n = jobs[i]
                try:
                    results[i] = fut.result(timeout=0)
                except Exception as e:
                    results[i] = {"ok": False, "agent": a.id, "node": n.id,
                                  "error": f"{type(e).__name__}: {e}"}
            for fut in not_done:
                i = index[fut]
                a, n = jobs[i]
                fut.cancel()
                results[i] = {"ok": False, "agent": a.id, "node": n.id,
                              "error": f"timeout after {timeout}s",
                              "timeout": True}
        finally:
            # Never block on stragglers: a stalled network call inside a
            # worker thread must not hold the whole objective hostage.
            ex.shutdown(wait=False, cancel_futures=True)
        return [r if r is not None else
                {"ok": False, "error": "no result produced", "node": jobs[i][1].id}
                for i, r in enumerate(results)]

    def status(self) -> Dict[str, Any]:
        return {"agents": [{"id": a.id, "role": a.role.value, "status": a.status,
                            "done": a.tasks_done, "failed": a.tasks_failed,
                            "last_error": (a.last_error or "")[:200]}
                           for a in self.agents.values()],
                "handoffs": self.handoffs[-20:]}
