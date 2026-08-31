"""
LIVE RECURSIVE DEPENDENCY GRAPH.

The graph is the live execution state of the objective and the single authority
on what must happen next. It represents goals, subgoals, tasks, subtasks,
capabilities, tools, skills, agents, models, files, documents, knowledge, data,
APIs, services, permissions, runtimes, workflows, platform requirements,
outputs, verification requirements and JIT workers.

It supports direct, transitive, recursive, optional and alternative
dependencies, substitutions, conflicts, cycles, blocked dependencies, and
resource / platform / runtime / skill / model / permission dependencies.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import state
from .models import (EdgeType, GraphEdge, GraphNode, NodeStatus, NodeType,
                     new_id)

# Node kinds that describe requirements rather than units of work. They are
# satisfied by resolution, not by execution, so the scheduler skips them.
NON_EXECUTABLE_TYPES = {
    NodeType.CAPABILITY, NodeType.PERMISSION, NodeType.RUNTIME, NodeType.PLATFORM,
    NodeType.MODEL, NodeType.SKILL, NodeType.AGENT, NodeType.KNOWLEDGE,
    NodeType.DATA, NodeType.API, NodeType.SERVICE, NodeType.FILE,
    NodeType.DOCUMENT, NodeType.OUTPUT, NodeType.WORKER, NodeType.WORKFLOW,
}


class DependencyGraph:
    def __init__(self, objective_id: str) -> None:
        self.objective_id = objective_id

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def add_node(self, type: NodeType | str, title: str, **kw) -> GraphNode:
        n = GraphNode(type=NodeType(type), title=title, objective_id=self.objective_id,
                      **kw)
        # Capability / permission / agent / runtime nodes describe requirements
        # that are RESOLVED when the plan is built. They are not units of work,
        # so they start satisfied - otherwise every task would be blocked
        # forever by a node the scheduler is designed to skip.
        nt = NodeType(type)
        if nt in NON_EXECUTABLE_TYPES and "status" not in kw:
            n.status = NodeStatus.SUCCEEDED
        state.upsert_node(n)
        state.emit("graph.node.added", "graph",
                   {"node": n.id, "type": n.type.value, "title": title},
                   self.objective_id)
        return n

    def add_edge(self, src: str, dst: str, type: EdgeType | str = EdgeType.DEPENDS_ON,
                 optional: bool = False, weight: float = 1.0,
                 meta: Optional[Dict[str, Any]] = None) -> GraphEdge:
        e = GraphEdge(src=src, dst=dst, type=EdgeType(type),
                      optional=optional or EdgeType(type) in (
                          EdgeType.OPTIONAL_DEPENDS_ON, EdgeType.ALTERNATIVE_OF),
                      weight=weight, meta=meta or {})
        state.upsert_edge(e)
        state.emit("graph.edge.added", "graph",
                   {"edge": e.id, "src": src, "dst": dst, "type": e.type.value},
                   self.objective_id)
        return e

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def nodes(self) -> List[GraphNode]:
        return state.nodes(self.objective_id)

    def node(self, nid: str) -> Optional[GraphNode]:
        return state.get_node(nid)

    def edges(self) -> List[GraphEdge]:
        ids = {n.id for n in self.nodes()}
        return [e for e in state.edges() if e.src in ids or e.dst in ids]

    def _out(self, nid: str) -> List[GraphEdge]:
        return [e for e in self.edges() if e.src == nid]

    def _in(self, nid: str) -> List[GraphEdge]:
        return [e for e in self.edges() if e.dst == nid]

    def dependencies(self, nid: str, include_optional: bool = False
                     ) -> List[Tuple[GraphEdge, GraphNode]]:
        out = []
        for e in self._out(nid):
            if e.type in (EdgeType.DEPENDS_ON, EdgeType.OPTIONAL_DEPENDS_ON,
                          EdgeType.REQUIRES_CAPABILITY, EdgeType.REQUIRES_PERMISSION,
                          EdgeType.REQUIRES_PLATFORM, EdgeType.REQUIRES_RUNTIME,
                          EdgeType.REQUIRES_RESOURCE):
                if e.optional and not include_optional:
                    continue
                n = self.node(e.dst)
                if n:
                    out.append((e, n))
        return out

    def dependents(self, nid: str) -> List[GraphNode]:
        return [n for e in self._in(nid) if (n := self.node(e.src))]

    def transitive_dependencies(self, nid: str) -> Set[str]:
        seen: Set[str] = set()
        q = deque([nid])
        while q:
            cur = q.popleft()
            for _e, dep in self.dependencies(cur, include_optional=True):
                if dep.id not in seen and dep.id != nid:
                    seen.add(dep.id)
                    q.append(dep.id)
        return seen

    def find_cycles(self) -> List[List[str]]:
        """Return cycles instead of crashing on them."""
        adj: Dict[str, List[str]] = defaultdict(list)
        for e in self.edges():
            if e.type in (EdgeType.DEPENDS_ON, EdgeType.REQUIRES_CAPABILITY,
                          EdgeType.REQUIRES_PERMISSION, EdgeType.REQUIRES_PLATFORM,
                          EdgeType.REQUIRES_RUNTIME, EdgeType.REQUIRES_RESOURCE):
                adj[e.src].append(e.dst)
        cycles: List[List[str]] = []
        color: Dict[str, int] = defaultdict(int)
        stack: List[str] = []

        def dfs(u: str) -> None:
            color[u] = 1
            stack.append(u)
            for v in adj.get(u, []):
                if color[v] == 0:
                    dfs(v)
                elif color[v] == 1:
                    i = stack.index(v) if v in stack else 0
                    cycles.append(stack[i:] + [v])
            stack.pop()
            color[u] = 2
        for n in adj:
            if color[n] == 0:
                dfs(n)
        return cycles

    def conflicts(self, nid: str) -> List[str]:
        return [e.dst for e in self._out(nid) if e.type == EdgeType.CONFLICTS_WITH] + \
               [e.src for e in self._in(nid) if e.type == EdgeType.CONFLICTS_WITH]

    # ------------------------------------------------------------------ #
    # Status propagation
    # ------------------------------------------------------------------ #
    def update(self, nid: str, **fields) -> GraphNode:
        n = self.node(nid)
        if not n:
            raise KeyError(nid)
        for k, v in fields.items():
            setattr(n, k, v)
        state.upsert_node(n)
        return n

    def mark(self, nid: str, status: NodeStatus, **fields) -> GraphNode:
        n = self.update(nid, status=status, **fields)
        if status == NodeStatus.RUNNING and n.started_at is None:
            n.started_at = time.time()
        if status in (NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED,
                      NodeStatus.CANCELLED, NodeStatus.SUBSTITUTED):
            n.finished_at = time.time()
        state.upsert_node(n)
        state.emit(f"graph.node.{status.value}", "graph",
                   {"node": nid, "title": n.title}, self.objective_id)
        return n

    def blocked_by(self, nid: str) -> List[Dict[str, Any]]:
        """Unsatisfied dependencies (recursive)."""
        out = []
        for e, dep in self.dependencies(nid):
            if dep.type in NON_EXECUTABLE_TYPES:
                continue
            if dep.status in (NodeStatus.SUCCEEDED, NodeStatus.SKIPPED):
                continue
            out.append({"node": dep.id, "title": dep.title, "status": dep.status.value,
                        "type": dep.type.value, "optional": e.optional,
                        "edge": e.type.value})
        return out

    def ready(self) -> List[GraphNode]:
        """Nodes whose dependencies are satisfied and that are not running yet."""
        out: List[GraphNode] = []
        running = {n.id for n in self.nodes() if n.status == NodeStatus.RUNNING}
        for n in self.nodes():
            if n.status not in (NodeStatus.PENDING, NodeStatus.BLOCKED,
                                NodeStatus.READY):
                continue
            if n.type in NON_EXECUTABLE_TYPES:
                continue
            # Anything that has not FINISHED blocks. Listing only the
            # unresolved states is what let a merely-queued ("ready")
            # dependency count as satisfied, which collapsed the whole graph
            # into one parallel batch and ran dependent steps together.
            blockers = [b for b in self.blocked_by(n.id)
                        if b["status"] not in ("succeeded", "skipped")]
            if blockers:
                if n.status != NodeStatus.BLOCKED:
                    self.mark(n.id, NodeStatus.BLOCKED)
                continue
            if any(c in running for c in self.conflicts(n.id)):
                continue
            if n.status != NodeStatus.READY:
                self.mark(n.id, NodeStatus.READY)
            out.append(n)
        return out

    def critical_path(self) -> List[str]:
        """Longest weighted dependency chain (by task duration where known)."""
        nodes = {n.id: n for n in self.nodes()}
        memo: Dict[str, float] = {}
        nxt: Dict[str, str] = {}

        def longest(nid: str) -> float:
            if nid in memo:
                return memo[nid]
            memo[nid] = 0.0
            best, best_id = 0.0, ""
            for e, dep in self.dependencies(nid):
                d = (dep.finished_at - dep.started_at) if (dep.finished_at and
                                                           dep.started_at) else e.weight
                cand = longest(dep.id) + max(d, e.weight)
                if cand > best:
                    best, best_id = cand, dep.id
            memo[nid] = best
            nxt[nid] = best_id
            return best

        for n in nodes:
            longest(n)
        start = max(memo, key=lambda k: memo[k]) if memo else ""
        path, cur = [], start
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            path.append(cur)
            cur = nxt.get(cur, "")
        return path

    # ------------------------------------------------------------------ #
    # Mutation: expand the graph when new requirements are discovered
    # ------------------------------------------------------------------ #
    def expand(self, parent_id: str, type: NodeType | str, title: str,
               capability: Optional[str] = None, agent: Optional[str] = None,
               edge: EdgeType | str = EdgeType.DEPENDS_ON, **kw) -> GraphNode:
        """Add a newly-discovered requirement under an existing node."""
        n = self.add_node(type, title, capability=capability, agent=agent, **kw)
        self.add_edge(parent_id, n.id, edge)
        state.emit("graph.expanded", "graph",
                   {"parent": parent_id, "child": n.id, "title": title,
                    "capability": capability}, self.objective_id)
        return n

    def substitute(self, failed_id: str, reason: str = "") -> Optional[GraphNode]:
        """
        Create a substitution path for a failed node:
          - prefer an explicit ALTERNATIVE_OF / SUBSTITUTES edge
          - otherwise re-target the same capability with a different component
        Dependents are rewired to the new node; the failed node is marked
        SUBSTITUTED so history is preserved.
        """
        failed = self.node(failed_id)
        if not failed:
            return None
        alts = [e.dst for e in self._out(failed_id)
                if e.type in (EdgeType.ALTERNATIVE_OF, EdgeType.SUBSTITUTES)]
        new = None
        if alts:
            new = self.node(alts[0])
            if new:
                self.update(new.id, status=NodeStatus.PENDING, attempts=0,
                            error=None)
        if new is None:
            new = self.add_node(
                failed.type, f"{failed.title} (substitute)",
                capability=failed.capability,
                payload={**failed.payload, "substitute_for": failed_id,
                         "exclude": (failed.payload.get("exclude") or []) +
                                    [failed.component] if failed.component else
                                    (failed.payload.get("exclude") or [])},
                agent=failed.agent, critical=failed.critical)
            self.add_edge(new.id, failed_id, EdgeType.SUBSTITUTES,
                          meta={"reason": reason})
        for dep_node in self.dependents(failed_id):
            for e in self._out(dep_node.id):
                if e.dst == failed_id and e.type == EdgeType.DEPENDS_ON:
                    self.add_edge(dep_node.id, new.id, EdgeType.DEPENDS_ON,
                                  weight=e.weight)
                    state.delete_edges([e.id])
        self.mark(failed_id, NodeStatus.SUBSTITUTED, error=reason)
        state.emit("graph.substituted", "graph",
                   {"failed": failed_id, "substitute": new.id, "reason": reason},
                   self.objective_id)
        return new

    # ------------------------------------------------------------------ #
    def parallel_groups(self) -> List[List[str]]:
        """Ready nodes grouped into sets that may safely run concurrently."""
        ready = self.ready()
        groups: List[List[str]] = []
        placed: Set[str] = set()
        conflict_map = {n.id: set(self.conflicts(n.id)) for n in ready}
        for n in ready:
            if n.id in placed:
                continue
            group = [n.id]
            placed.add(n.id)
            for m in ready:
                if m.id in placed:
                    continue
                if m.id in conflict_map[n.id] or n.id in conflict_map[m.id]:
                    continue
                # same underlying component => serialise to respect concurrency
                if n.component and m.component and n.component == m.component:
                    continue
                group.append(m.id)
                placed.add(m.id)
            groups.append(group)
        return groups

    # ------------------------------------------------------------------ #
    def progress(self) -> Dict[str, Any]:
        ns = self.nodes()
        by_status: Dict[str, int] = defaultdict(int)
        for n in ns:
            by_status[n.status.value] += 1
        total = len([n for n in ns if n.type in (NodeType.TASK, NodeType.SUBTASK,
                                                 NodeType.GOAL, NodeType.SUBGOAL)])
        done = by_status.get("succeeded", 0) + by_status.get("skipped", 0)
        return {"total_nodes": len(ns), "by_status": dict(by_status),
                "executable_total": total,
                "completed": done,
                "percent": round(100.0 * done / total, 1) if total else 0.0,
                "cycles": len(self.find_cycles()),
                "critical_path": self.critical_path()[:10]}

    def snapshot(self) -> Dict[str, Any]:
        return {"objective_id": self.objective_id,
                "nodes": [n.model_dump() for n in self.nodes()],
                "edges": [e.model_dump() for e in self.edges()],
                "progress": self.progress()}
