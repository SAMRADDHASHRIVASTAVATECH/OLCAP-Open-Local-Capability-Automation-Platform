"""
UNIFIED CORE.

The authoritative controller for goals, decomposition, success criteria,
planning, orchestration, capability discovery and selection, skill selection,
tool selection, dependency resolution, JIT activation, scheduling, state,
memory, knowledge, events, permissions, failure recovery, verification,
replanning, resource-aware scheduling and platform-aware selection.

No agent framework is the master controller. The graph is authoritative.

    DISCOVER -> RESOLVE -> PLAN -> EXECUTE -> OBSERVE -> UPDATE GRAPH ->
    REPLAN -> CONTINUE
"""
from __future__ import annotations

import itertools
import json
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import state
from .agents import AgentRole, AgentSystem
from .config import cfg
from .graph import DependencyGraph
from .models import (EdgeType, GraphNode, NodeStatus, NodeType, PermissionCategory)
from .observability import span
from .registry import registry

# --------------------------------------------------------------------------- #
# Intent -> required capabilities. Used by the planner to build the graph.
# The graph then decides ordering, not this table.
# --------------------------------------------------------------------------- #
# Monotonic suffix so objective ids stay unique within the same millisecond.
_OBJ_SEQ = itertools.count(1)

INTENT_RULES: List[tuple] = [
    (r"\b(research|investigate|study|analyse the evidence|deep dive|"
     r"literature|state of the art|compare approaches)\b",
     ["RESEARCH", "SOURCE_VERIFICATION"]),
    (r"\b(search|look up|find|google|web)\b",
     ["WEB_SEARCH", "WEB_EXTRACT"]),
    (r"\b(crawl|scrape|follow links|harvest)\b",
     ["WEB_CRAWL", "WEB_EXTRACT"]),
    (r"\b(browse|open the page|navigate to|screenshot the site|click)\b",
     ["WEB_BROWSE", "WEB_INTERACT"]),
    (r"\b(read|parse|extract).*(pdf|docx?|document|file)|\bpdf\b|\bdocx?\b",
     ["DOCUMENT_INTELLIGENCE", "RAG"]),
    (r"\b(summarise|summarize|notes|knowledge base|index)\b",
     ["RAG", "KNOWLEDGE_SEARCH"]),
    (r"\b(remember|recall|store this|memory)\b", ["MEMORY"]),
    (r"\b(analys[ei]s|statistics|csv|parquet|dataset|aggregate|chart)\b",
     ["DATA_ANALYSIS", "DATABASE_QUERY"]),
    (r"\b(sql|query the database|duckdb|postgres)\b", ["DATABASE_QUERY"]),
    (r"\b(automate|workflow|schedule|durable|pipeline|every day)\b",
     ["WORKFLOW_EXECUTION", "DURABLE_TASKS"]),
    (r"\b(run|execute|command|terminal|shell|powershell|bash)\b", ["TERMINAL"]),
    (r"\b(file|folder|directory|save to disk|write file|copy|move)\b", ["FILESYSTEM"]),
    # "write a short note to /tmp/x.md" / "save the report as notes.txt"
    (r"(?i)\b(write|save|store|dump|put|persist|export)\b[^.\n]{0,60}?"
     r"\b(to|into|as|at)\b[^.\n]{0,40}?"
     r"([/\\~][\w./\\-]*|\b[\w-]+\.(?:md|txt|json|csv|ya?ml|html?)\b)",
     ["FILESYSTEM"]),
    # any explicit filename extension implies file work
    (r"(?i)\b[\w-]+\.(?:md|txt|json|csv|ya?ml|html?)\b", ["FILESYSTEM"]),
    (r"\b(window|gui|click|keyboard|mouse|screen)\b", ["WINDOWS_CONTROL", "GUI"]),
    (r"\b(screenshot|capture the screen)\b", ["SCREENSHOT"]),
    (r"(?<![\w-])(processes|task manager|kill the process)\b|"
     r"\b(list processes|start process|stop process)\b", ["PROCESS_CONTROL"]),
    (r"\b(verify|check that|validate|test the result)\b", ["VERIFICATION"]),
]

# Wall-clock budget for one objective. Overridable per call via max_seconds
# and via the OLCAP_RUN_SECONDS environment variable.
DEFAULT_RUN_SECONDS = float(os.environ.get("OLCAP_RUN_SECONDS", "300") or 300)

DEFAULT_CRITERIA = [
    {"name": "produced_output", "kind": "min_length", "value": 80},
]


class UnifiedCore:
    def __init__(self, objective_id: Optional[str] = None) -> None:
        self.c = cfg()
        # Uniqueness must survive many objectives created in the same second
        # (concurrent goals), so the id is timestamp + a monotonic counter +
        # a short random suffix -- never timestamp alone.
        self.objective_id = objective_id or (
            f"obj_{int(time.time() * 1000):013d}_{next(_OBJ_SEQ):04d}_"
            f"{uuid.uuid4().hex[:6]}")
        self.graph = DependencyGraph(self.objective_id)
        self.agents = AgentSystem(self.objective_id)
        self.reg = registry()

    # ------------------------------------------------------------------ #
    # 1. DISCOVER + PLAN
    # ------------------------------------------------------------------ #
    def plan(self, objective: str, success_criteria: Optional[List[Any]] = None,
             use_model: bool = True) -> Dict[str, Any]:
        objective = (objective or "").strip()
        if not objective:
            raise ValueError("objective is required")
        state.set_kv(f"objective.{self.objective_id}.text", objective)
        state.set_kv(f"objective.{self.objective_id}.criteria",
                     success_criteria or DEFAULT_CRITERIA)
        state.set_kv("current_objective", self.objective_id)

        caps = self.discover_capabilities(objective)
        model_plan = None
        if use_model:
            model_plan = self._model_plan(objective, caps)

        goal = self.graph.add_node(NodeType.GOAL, objective, critical=True,
                                   payload={"criteria": success_criteria or DEFAULT_CRITERIA})
        prev_task: Optional[GraphNode] = None
        for cap in caps:
            sub = self.graph.add_node(NodeType.SUBGOAL, f"{cap}",
                                      capability=cap, critical=True)
            self.graph.add_edge(goal.id, sub.id, EdgeType.DEPENDS_ON)

            cap_node = self.graph.add_node(NodeType.CAPABILITY, f"capability:{cap}",
                                           capability=cap)
            self.graph.add_edge(sub.id, cap_node.id, EdgeType.REQUIRES_CAPABILITY)

            agent_role = self._role_for(cap)
            agent_node = self.graph.add_node(NodeType.AGENT, f"agent:{agent_role}",
                                             capability=cap, agent=agent_role)
            self.graph.add_edge(sub.id, agent_node.id, EdgeType.ASSIGNED_AGENT)

            for perm in (self.reg.capability(cap).permissions if self.reg.capability(cap) else []):
                pnode = self.graph.add_node(NodeType.PERMISSION, f"permission:{perm.value}",
                                            capability=cap)
                self.graph.add_edge(sub.id, pnode.id, EdgeType.REQUIRES_PERMISSION)

            if prev_task is not None:
                self.graph.add_edge(sub.id, prev_task.id, EdgeType.DEPENDS_ON,
                                    weight=0.5)
            prev_task = sub

        # A node that writes a file must run AFTER the nodes that produce its
        # content. Without these edges it lands in the same parallel batch as
        # the research steps and writes a file containing nothing but the
        # restated objective.
        _writers = {"FILESYSTEM"}
        _producers = {"WEB_SEARCH", "WEB_EXTRACT", "WEB_CRAWL", "WEB_BROWSE",
                      "RESEARCH", "SOURCE_VERIFICATION", "SOURCE_DISCOVERY",
                      "RAG", "KNOWLEDGE_SEARCH", "DOCUMENT_INTELLIGENCE",
                      "DATA_ANALYSIS", "DATABASE_QUERY"}
        subgoals = [n for n in self.graph.nodes() if n.type == NodeType.SUBGOAL]
        for w in subgoals:
            if (w.capability or "") not in _writers:
                continue
            for prod in subgoals:
                if prod.id == w.id or (prod.capability or "") not in _producers:
                    continue
                try:
                    self.graph.add_edge(w.id, prod.id, EdgeType.DEPENDS_ON,
                                        weight=0.4)
                except Exception:
                    pass

        verify = self.graph.add_node(NodeType.VERIFICATION, "verify outcome",
                                     critical=True,
                                     payload={"criteria": success_criteria or DEFAULT_CRITERIA})
        self.graph.add_edge(goal.id, verify.id, EdgeType.DEPENDS_ON)
        if prev_task is not None:
            self.graph.add_edge(verify.id, prev_task.id, EdgeType.DEPENDS_ON)

        state.emit("core.planned", "core",
                   {"objective": objective[:160], "capabilities": caps,
                    "model_plan": bool(model_plan)}, self.objective_id)
        return {"objective_id": self.objective_id, "capabilities": caps,
                "goal_node": goal.id, "model_assisted": bool(model_plan),
                "graph": self.graph.progress()}

    def discover_capabilities(self, objective: str) -> List[str]:
        text = objective.lower()
        found: List[str] = []
        for pat, caps in INTENT_RULES:
            if re.search(pat, text):
                for cap in caps:
                    if cap not in found:
                        found.append(cap)
        # Drop capabilities the planner cannot parameterise from the objective
        # alone - adding them would create nodes that can only fail.
        found = [c for c in found if self._can_parameterise(c, text)]
        if not found:
            found = ["RESEARCH", "SOURCE_VERIFICATION"]
        # ensure verification whenever the objective asks for completion
        if re.search(r"\b(verify|complete|actually complete|test it)\b", text) \
                and "VERIFICATION" not in found:
            found.append("VERIFICATION")
        return found

    @staticmethod
    def _can_parameterise(cap: str, text: str) -> bool:
        import re as _re
        if cap == "DATABASE_QUERY":
            return bool(_re.search(r"\bselect\b|\.parquet\b|\.duckdb\b|"
                                   r"\.csv\b|\.sqlite|\.db\b|\bsql\b", text))
        if cap == "DOCUMENT_INTELLIGENCE":
            return bool(_re.search(r"[\w./\\-]+\.(pdf|docx?|txt|md|csv|html|json)",
                                   text))
        if cap == "PROCESS_CONTROL":
            return bool(_re.search(r"\b(processes|task manager|running processes)\b",
                                   text))
        if cap in ("WINDOW_MANAGEMENT", "GUI", "SCREENSHOT"):
            return True
        return True

    def _upstream_urls(self, node: GraphNode) -> List[str]:
        """URLs discovered by upstream nodes - the graph feeds the next step."""
        import re as _re
        urls: List[str] = []
        for _e, dep in self.graph.dependencies(node.id, include_optional=True):
            blob = json.dumps(dep.result or {}, default=str)
            found = _re.findall(r"https?://[^\"\\ ]+", blob)
            urls.extend(u.rstrip('",') for u in found)
        seen, out = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    @staticmethod
    def _objective_path(objective: str) -> str:
        """First explicit path in the objective (the destination it named)."""
        pats = [r"[/\\][\w./\\-]*[/\\][\w.-]+",        # /tmp/a/b.md
                r"~[\\/][\w./\\-]+",                      # ~/dir/file
                r"[A-Za-z]:\\\[^\\s]+",                       # C:\\dir\\file
                r"[\w.-]+\.(?:md|txt|json|csv|ya?ml|html?)"]  # notes.md
        for pat in pats:
            m = re.search(pat, objective or "")
            if m:
                return m.group(0).rstrip(".,;")
        return ""

    def _upstream_text(self, node: GraphNode) -> str:
        """Real text produced by upstream nodes, so a written note contains
        what the system actually found instead of restating the objective."""
        chunks: List[str] = []
        for _e, dep in self.graph.dependencies(node.id, include_optional=True):
            chunks.extend(self._node_text(dep))
        if not chunks:
            # The write node is often a sibling, not a dependent, of the
            # research nodes. Fall back to everything the objective has
            # produced so far - otherwise the file just restates the request.
            for other in self.graph.nodes():
                if other.id == node.id:
                    continue
                if str(getattr(other, "status", "")).lower() not in (
                        "nodestatus.succeeded", "succeeded"):
                    continue
                chunks.extend(self._node_text(other))
        out = "\n\n".join(dict.fromkeys(chunks)).strip()
        return out[:8000]

    def _node_text(self, node: GraphNode) -> List[str]:
        """Human-readable text a node produced, if any."""
        out: List[str] = []
        art = (node.result or {}).get("artifact_id")
        data = None
        if art:
            rec = state.artifact(art)
            if rec and rec.get("content"):
                try:
                    data = json.loads(rec["content"])
                except Exception:
                    data = rec["content"]
        data = data if data not in (None, "") else (node.result or {})
        try:
            obj = json.loads(data) if isinstance(data, str) else data
        except Exception:
            obj = None
        if isinstance(obj, dict):
            inner = (obj.get("output") or {}).get("result") if isinstance(
                obj.get("output"), dict) else None
            # The common shapes: {result:{text:...}}, {output:{result:{...}}},
            # or the payload directly. Search all of them before giving up and
            # dumping raw JSON into the user's file.
            sources = []
            for cand in (obj.get("result"), inner, obj):
                if isinstance(cand, dict) and cand not in sources:
                    sources.append(cand)
            for key in ("report", "answer", "text", "content", "summary"):
                for src in sources:
                    if isinstance(src, dict) and src.get(key):
                        out.append(str(src[key])[:4000])
                        break
                if out:
                    break
        if not out:
            blob = json.dumps(data, default=str)
            if blob not in ("{}", '""', ""):
                out.append(blob[:3000])
        return out

    def _model_plan(self, objective: str, caps: List[str]) -> Optional[Dict[str, Any]]:
        """
        Ask the model (through OpenCode's model path) for success criteria and
        any requirement we may have missed. Never authoritative: if it fails or
        is unreachable we continue with the deterministic plan.
        """
        try:
            from .llm import llm
            res = llm().complete(
                system="Return ONLY compact JSON.",
                prompt=(f"Objective: {objective}\n"
                        f"Planned capabilities: {caps}\n"
                        "Return JSON: {\"success_criteria\": [<=5 short strings], "
                        "\"missing_capabilities\": [subset of "
                        "WEB_SEARCH,WEB_BROWSE,WEB_CRAWL,WEB_EXTRACT,WEB_INTERACT,"
                        "RESEARCH,KNOWLEDGE_SEARCH,RAG,MEMORY,DOCUMENT_INTELLIGENCE,"
                        "SOURCE_VERIFICATION,DATA_ANALYSIS,DATABASE_QUERY,"
                        "VECTOR_STORAGE,WORKFLOW_EXECUTION,DURABLE_TASKS,COMPUTER_USE,"
                        "WINDOWS_CONTROL,FILESYSTEM,PROCESS_CONTROL,TERMINAL,GUI,"
                        "SCREENSHOT,WINDOW_MANAGEMENT]}"))
            import json
            # A degraded answer (no model reachable, or the deterministic
            # extractive fallback) is not a judgement about this objective:
            # acting on it would silently invent success criteria and then
            # report the run as "verified" against them.
            degraded = bool(res.get("degraded")) or \
                res.get("backend") in ("stub", "stub_fallback")
            if degraded:
                state.set_kv(f"objective.{self.objective_id}.model_degraded",
                             {"backend": res.get("backend"),
                              "error": (res.get("error") or "")[:200]})
                state.emit("core.model_degraded", "core",
                           {"backend": res.get("backend")}, self.objective_id)
                return None
            txt = res.get("text", "")
            m = re.search(r"\{.*\}", txt, re.S)
            if not m:
                return None
            data = json.loads(m.group(0))
            for cap in (data.get("missing_capabilities") or []):
                if cap not in caps and self.reg.capability(cap):
                    caps.append(cap)
                    state.emit("core.model_expanded", "core",
                               {"capability": cap}, self.objective_id)
            if data.get("success_criteria"):
                state.set_kv(f"objective.{self.objective_id}.criteria",
                             data["success_criteria"])
            return data
        except Exception:
            return None

    @staticmethod
    def _role_for(cap: str) -> str:
        table = {
            "RESEARCH": AgentRole.RESEARCHER, "SOURCE_VERIFICATION": AgentRole.VERIFIER,
            "WEB_SEARCH": AgentRole.RESEARCHER, "WEB_EXTRACT": AgentRole.BROWSER,
            "WEB_CRAWL": AgentRole.BROWSER, "WEB_BROWSE": AgentRole.BROWSER,
            "WEB_INTERACT": AgentRole.BROWSER,
            "DOCUMENT_INTELLIGENCE": AgentRole.DOCUMENT, "RAG": AgentRole.DOCUMENT,
            "KNOWLEDGE_SEARCH": AgentRole.DOCUMENT, "MEMORY": AgentRole.PLANNER,
            "DATA_ANALYSIS": AgentRole.DATA, "DATABASE_QUERY": AgentRole.DATA,
            "VECTOR_STORAGE": AgentRole.DATA,
            "WORKFLOW_EXECUTION": AgentRole.AUTOMATION,
            "DURABLE_TASKS": AgentRole.AUTOMATION,
            "FILESYSTEM": AgentRole.CODER, "TERMINAL": AgentRole.CODER,
            "PROCESS_CONTROL": AgentRole.AUTOMATION,
            "WINDOWS_CONTROL": AgentRole.AUTOMATION, "GUI": AgentRole.AUTOMATION,
            "SCREENSHOT": AgentRole.AUTOMATION,
            "WINDOW_MANAGEMENT": AgentRole.AUTOMATION, "COMPUTER_USE": AgentRole.AUTOMATION,
            "VERIFICATION": AgentRole.VERIFIER, "OBSERVABILITY": AgentRole.VERIFIER,
        }
        return str(table.get(cap, AgentRole.RESEARCHER).value)

    # ------------------------------------------------------------------ #
    # 2. EXECUTE LOOP
    # ------------------------------------------------------------------ #
    def _invoke(self, capability: str, params: Dict[str, Any],
                ctx: Dict[str, Any]) -> Dict[str, Any]:
        from .runtime import execute
        return execute(capability, params, ctx=ctx, objective_id=self.objective_id)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _search_query(objective: str, attempt: int = 1) -> str:
        """
        Turn an imperative objective into a query an engine can answer.
        "Search the web for DuckDB and remember X, then verify it" -> "DuckDB".
        On later attempts the query is progressively widened so a retry is
        never just a repeat of the same failed request.
        """
        import re as _re
        q = (objective or "").strip()
        q = _re.sub(r"^(?:please\s+)?(?:search(?:\s+the\s+web|\s+online)?\s+for|"
                    r"look\s+up|find(?:\s+out)?|research|investigate|study|"
                    r"analyse|analyze|gather\s+information\s+on|tell\s+me\s+about)\s+",
                    "", q, flags=_re.I)
        q = _re.split(r"\s+(?:and|then|afterwards|finally)\s+", q, maxsplit=1)[0]
        q = _re.split(r",\s*(?:then|and)\s+", q, maxsplit=1)[0]
        q = q.strip(" .?!\"'")
        if attempt >= 3:
            q = " ".join(q.split()[:3])
        elif attempt == 2:
            q = " ".join(q.split()[:6])
        return q or (objective or "")[:80]

    def _payload_for(self, node: GraphNode, objective: str) -> Dict[str, Any]:
        cap = node.capability or ""
        base = dict(node.payload or {})
        attempt = int(getattr(node, "attempts", 1) or 1)
        if cap in ("WEB_SEARCH", "SOURCE_DISCOVERY", "SOURCE_COMPARISON"):
            base.setdefault("query", self._search_query(objective, attempt))
            base.setdefault("max_results", 10)
        elif cap == "WEB_CRAWL":
            urls = re.findall(r"https?://\S+", objective) or self._upstream_urls(node)
            base.setdefault("url", urls[0] if urls else "")
            base.setdefault("max_pages", 10)
        elif cap in ("WEB_BROWSE", "WEB_EXTRACT", "WEB_INTERACT"):
            urls = re.findall(r"https?://\S+", objective) or self._upstream_urls(node)
            base.setdefault("url", urls[0] if urls else "")
        elif cap == "RESEARCH":
            base.setdefault("question", objective if attempt == 1
                            else self._search_query(objective, attempt))
            base.setdefault("depth", 2)
            base.setdefault("max_sources", 8)
        elif cap in ("RAG", "KNOWLEDGE_SEARCH"):
            base.setdefault("action", "query")
            base.setdefault("query", objective)
        elif cap == "MEMORY":
            base.setdefault("action", "search")
            base.setdefault("query", objective)
        elif cap == "DOCUMENT_INTELLIGENCE":
            paths = re.findall(r"[\w./\\-]+\.(?:pdf|docx?|txt|md|html)", objective)
            base.setdefault("action", "extract")
            base.setdefault("path", paths[0] if paths else "")
        elif cap == "FILESYSTEM":
            path = self._objective_path(objective)
            if path:
                base.setdefault("action", "write")
                base.setdefault("path", path)
                base.setdefault("content", self._upstream_text(node) or objective)
            else:
                base.setdefault("action", "list")
                base.setdefault("path", str(cfg().home))
        elif cap == "TERMINAL":
            base.setdefault("command", objective)
        elif cap == "VERIFICATION":
            base.setdefault("criteria", state.get_kv(
                f"objective.{self.objective_id}.criteria", DEFAULT_CRITERIA))
        elif cap == "SOURCE_VERIFICATION":
            base.setdefault("claim", self._search_query(objective, attempt))
            base.setdefault("min_sources", 2)
        return base

    def run(self, objective: Optional[str] = None,
            success_criteria: Optional[List[Any]] = None,
            max_iterations: int = 40, parallel: bool = True,
            use_model: bool = True,
            max_seconds: float = DEFAULT_RUN_SECONDS) -> Dict[str, Any]:
        """Execute the plan.

        `max_seconds` is a hard wall-clock budget. Without it, one stalled
        network call keeps an objective running indefinitely - which is
        exactly the failure a long-running agent must never have.
        """
        if objective:
            self.plan(objective, success_criteria, use_model=use_model)
        objective_text = state.get_kv(f"objective.{self.objective_id}.text", objective or "")
        started = time.time()
        deadline = started + max(1.0, float(max_seconds))
        # Publish the budget so no single capability call can outlive the run.
        from . import runtime as _runtime
        _runtime.set_deadline(deadline)
        timed_out = False
        iteration = 0

        try:
            with span("core.run", "core", {"objective": str(objective_text)[:120]}) as sp:
                last_progress = self._progress()
                fruitless = 0
                stalled = False
                for iteration in range(1, max_iterations + 1):
                    if time.time() >= deadline:
                        timed_out = True
                        break
                    ready = self.graph.ready()
                    if not ready:
                        break
                    groups = self.graph.parallel_groups() if parallel else [[n.id] for n in ready]
                    for group in groups:
                        nodes = [n for n in ready if n.id in group]
                        if time.time() >= deadline:
                            timed_out = True
                            break
                        if parallel and len(nodes) > 1:
                            self._run_batch_parallel(
                                nodes, objective_text,
                                timeout=max(1.0, deadline - time.time()))
                        else:
                            for n in nodes:
                                if time.time() >= deadline:
                                    timed_out = True
                                    break
                                self._run_node(n, objective_text)
                    # OBSERVE -> UPDATE GRAPH -> REPLAN
                    self._discover_new_requirements(objective_text, iteration)

                    # Progress guard. An objective that produces nothing new
                    # twice in a row is not going to succeed on the third try,
                    # and continuing only burns the budget on work that cannot
                    # help - the difference between a 3-minute stall and a
                    # 15-second honest stop.
                    progress = self._progress()
                    if progress > last_progress:
                        fruitless = 0
                    else:
                        fruitless += 1
                    last_progress = max(last_progress, progress)
                    if fruitless >= 2:
                        stalled = True
                        state.emit("core.run.no_progress", "core",
                                   {"iterations": iteration,
                                    "succeeded": progress[0],
                                    "artifacts": progress[1]},
                                   self.objective_id)
                        break

                if timed_out:
                    # Say plainly which work was abandoned instead of leaving
                    # nodes pending and pretending they are still running.
                    for n in self.graph.ready():
                        self.graph.mark(n.id, NodeStatus.SKIPPED,
                                        error="run deadline exceeded")
                    state.emit("core.run.deadline", "core",
                               {"objective_id": self.objective_id,
                                "seconds": max_seconds}, self.objective_id)

                result = self._finalise(objective_text)
                result["deadline_exceeded"] = timed_out
                result["stalled"] = stalled
                result["elapsed_s"] = round(time.time() - started, 2)
                sp.set(iterations=iteration, completed=result.get("completed"),
                       timeout=timed_out)
                _runtime.clear_deadline(deadline)
                return result
        except BaseException:
            _runtime.clear_deadline(deadline)
            raise

    # ------------------------------------------------------------------ #
    def _progress(self) -> Tuple[int, int]:
        """(succeeded nodes, artifacts) - the two things that mean progress."""
        try:
            ns = self.graph.nodes()
            ok = len([n for n in ns if n.status == NodeStatus.SUCCEEDED])
            return ok, len(state.artifacts())
        except Exception:                                # noqa: BLE001
            return 0, 0

    def _run_node(self, node: GraphNode, objective: str) -> None:
        if node.type in (NodeType.CAPABILITY, NodeType.PERMISSION,
                         NodeType.AGENT, NodeType.PLATFORM, NodeType.RUNTIME):
            self.graph.mark(node.id, NodeStatus.SUCCEEDED)
            return
        # mark() returns the persisted node: keep the local object in sync or a
        # later upsert would overwrite the attempt counter with the stale value.
        node = self.graph.mark(node.id, NodeStatus.RUNNING,
                               attempts=node.attempts + 1)
        agent = self.agents.pick(node.capability)
        payload = self._payload_for(node, objective)
        node.payload = payload
        state.upsert_node(node)

        out = self.agents.run(agent, node, self._invoke)
        if out.get("ok"):
            # mark() re-reads and returns the persisted node. Reusing the stale
            # object would write the old status back over "succeeded".
            node = self.graph.mark(node.id, NodeStatus.SUCCEEDED,
                                   result=out["output"])
            aid = state.put_artifact("task_result", f"{node.capability or node.title}",
                                     content=out["output"],
                                     objective_id=self.objective_id,
                                     node_id=node.id,
                                     meta={"agent": agent.id,
                                           "capability": node.capability})
            node.result["artifact_id"] = aid
            state.upsert_node(node)
            # dependency satisfied -> dependents become ready
            for dep in self.graph.dependents(node.id):
                if dep.status == NodeStatus.BLOCKED:
                    self.graph.mark(dep.id, NodeStatus.PENDING)
        else:
            self._handle_failure(node, out.get("error", "unknown"), agent)

    def _run_batch_parallel(self, nodes: List[GraphNode], objective: str,
                            timeout: Optional[float] = None) -> None:
        for i, n in enumerate(nodes):
            nodes[i] = self.graph.mark(n.id, NodeStatus.RUNNING,
                                       attempts=n.attempts + 1)
        jobs = []
        for n in nodes:
            agent = self.agents.pick(n.capability)
            n.payload = self._payload_for(n, objective)
            state.upsert_node(n)
            jobs.append((agent, n))
        results = self.agents.run_parallel(
            jobs, self._invoke,
            max_workers=int(cfg().env("MAX_CONCURRENCY", "4")),
            timeout=timeout)
        for (agent, node), res in zip(jobs, results):
            if res.get("ok"):
                node = self.graph.mark(node.id, NodeStatus.SUCCEEDED,
                                       result=res["output"])
                aid = state.put_artifact("task_result", f"{node.capability or node.title}",
                                         content=res["output"],
                                         objective_id=self.objective_id,
                                         node_id=node.id,
                                         meta={"agent": agent.id, "parallel": True})
                node.result["artifact_id"] = aid
                state.upsert_node(node)
            else:
                self._handle_failure(node, res.get("error", "unknown"), agent)

    # ------------------------------------------------------------------ #
    def _handle_failure(self, node: GraphNode, error: str, agent: Any) -> None:
        from .recovery import recover
        node.error = error
        state.upsert_node(node)
        if node.attempts < node.max_attempts and \
                not re.search(r"(?i)permission", error):
            rec = recover(node.id, error, node.component, self.graph, node.attempts)
            if rec.get("action") == "substituted":
                return
            if rec.get("action") in ("retry", "repaired_component", "released_workers"):
                self.graph.mark(node.id, NodeStatus.PENDING)
                return
        self.graph.mark(node.id, NodeStatus.FAILED, error=error[:500])
        state.emit("core.node_failed", "core",
                   {"node": node.id, "title": node.title, "error": error[:200]},
                   self.objective_id)

    # ------------------------------------------------------------------ #
    # 3. DISCOVER new requirements from real observations
    # ------------------------------------------------------------------ #
    def _discover_new_requirements(self, objective: str, iteration: int) -> None:
        """
        If the observed results expose another requirement, EXPAND THE GRAPH.
        This is how capability combinations emerge, without hard-coded chains.
        """
        ns = {n.id: n for n in self.graph.nodes()}
        succeeded = {n.capability for n in ns.values()
                     if n.status == NodeStatus.SUCCEEDED and n.capability}
        existing = {n.capability for n in ns.values() if n.capability}
        planned_types = {(n.type.value, n.capability) for n in ns.values()}

        additions: List[tuple] = []
        # Search results are unverified by definition: once we have searched or
        # researched, verifying the sources becomes a real requirement.
        if ("RESEARCH" in succeeded or "WEB_SEARCH" in succeeded) \
                and "SOURCE_VERIFICATION" not in existing:
            additions.append(("SOURCE_VERIFICATION", "verify discovered sources"))
        if "WEB_CRAWL" in succeeded and "WEB_EXTRACT" not in existing:
            additions.append(("WEB_EXTRACT", "extract crawled pages"))
        if "DOCUMENT_INTELLIGENCE" in succeeded and "RAG" not in existing:
            additions.append(("RAG", "index documents"))
        if ("RESEARCH" in succeeded or "WEB_SEARCH" in succeeded) \
                and "MEMORY" not in existing and re.search(r"\b(remember|memory)\b",
                                                           objective.lower()):
            additions.append(("MEMORY", "persist findings"))
        if succeeded and "VERIFICATION" not in existing and \
                re.search(r"\b(verify|complete|test it)\b", objective.lower()):
            additions.append(("VERIFICATION", "verify final outcome"))

        for cap, title in additions:
            if (NodeType.SUBGOAL.value, cap) in planned_types:
                continue
            goal = next((n for n in ns.values() if n.type == NodeType.GOAL), None)
            parent = goal.id if goal else list(ns)[0]
            node = self.graph.expand(parent, NodeType.SUBGOAL, title, capability=cap,
                                     agent=self._role_for(cap))
            self.graph.expand(node.id, NodeType.CAPABILITY, f"capability:{cap}",
                              capability=cap, edge=EdgeType.REQUIRES_CAPABILITY)
            state.emit("core.graph_expanded", "core",
                       {"capability": cap, "title": title, "iteration": iteration},
                       self.objective_id)

    # ------------------------------------------------------------------ #
    # 4. VERIFY + FINALISE
    # ------------------------------------------------------------------ #
    def _finalise(self, objective: str) -> Dict[str, Any]:
        ns = self.graph.nodes()
        failed = [n for n in ns if n.status == NodeStatus.FAILED]
        succeeded = [n for n in ns if n.status == NodeStatus.SUCCEEDED
                     and n.type in (NodeType.SUBGOAL, NodeType.TASK)]
        results = []
        for n in succeeded:
            art = n.result.get("artifact_id")
            if art and state.artifact(art):
                results.append({"node": n.id, "capability": n.capability,
                                "title": n.title, "artifact_id": art,
                                "result": (state.artifact(art) or {}).get("content")})

        criteria = state.get_kv(f"objective.{self.objective_id}.criteria",
                                DEFAULT_CRITERIA)
        degraded_model = state.get_kv(
            f"objective.{self.objective_id}.model_degraded") or None
        combined = {"sources": [], "report": "", "artifacts": len(results),
                    "results": results, "capabilities": sorted(
                        {r["capability"] for r in results})}
        for r in results:
            try:
                import json as _json
                data = _json.loads(r["result"]) if isinstance(r["result"], str) else r["result"]
                out = (data or {}).get("output", {}).get("result", {}) \
                    if isinstance(data, dict) else {}
                if isinstance(out, dict):
                    combined["sources"].extend(out.get("sources", []) or [])
                    if out.get("report"):
                        combined["report"] += str(out["report"])[:4000] + "\n\n"
            except Exception:
                pass

        from .verification import verify_artifact

        # Every declared criterion is evaluated - string criteria included.
        # (verify_artifact scores a bare string by term overlap against the
        # produced output.) They used to be built and then thrown away in
        # favour of a single length check, which let any output "pass".
        if isinstance(criteria, list) and criteria:
            crit = list(criteria)
        else:
            crit = list(DEFAULT_CRITERIA)
        # Baseline: something real was produced, whatever the criteria say.
        crit = crit + [{"name": "output_produced", "kind": "min_length",
                        "value": 120}]
        verification = verify_artifact({"criteria": crit, "result": combined})

        # Relevance is reported, never used as a gate: a term-overlap gate
        # fails legitimate work whose output is metadata rather than prose,
        # and passes an impossible objective simply because the report echoes
        # the wording. Honest reporting beats a flaky pass/fail.
        try:
            from .verification import _distinctive_terms
            terms = _distinctive_terms(objective)
            blob = json.dumps(combined, default=str).lower()
            hits = [t for t in terms if t in blob]
            relevance = round(len(hits) / len(terms), 3) if terms else None
        except Exception:
            terms, hits, relevance = [], [], None

        # Completion and verification are different claims:
        #   completed = all planned work finished without a failure
        #   verified  = it was checked against objective-specific criteria
        criteria_are_default = criteria is None or (
            isinstance(criteria, list) and
            all(isinstance(x, dict) and x.get("name") in
                {d.get("name") for d in DEFAULT_CRITERIA} for x in criteria))
        completed = bool(results) and not failed and bool(
            verification.get("passed"))
        report = {
            "objective_id": self.objective_id, "objective": objective,
            "completed": completed,
            "verified": bool(completed and not criteria_are_default),
            "criteria_source": "default" if criteria_are_default else "objective",
            "relevance": relevance,
            "relevance_terms": {"total": len(terms), "matched": len(hits)},
            "model_degraded": bool(degraded_model),
            "model_backend": (degraded_model or {}).get("backend"),
            "notice": ("no objective-specific success criteria were available "
                       "- verification is generic") if criteria_are_default
                      else ("planning used a degraded model fallback "
                            f"({(degraded_model or {}).get('backend')}); success "
                            "criteria are generic")
                      if degraded_model else "",
            "iterations_used": None,
            "nodes_total": len(ns), "nodes_succeeded": len(succeeded),
            "nodes_failed": len([n for n in failed]),
            "failed_nodes": [{"id": n.id, "title": n.title,
                              "error": (n.error or "")[:200]} for n in failed],
            "capabilities_used": sorted({n.capability for n in succeeded
                                         if n.capability}),
            "verification": verification,
            "results": results,
            "graph": self.graph.progress(),
            "agents": self.agents.status(),
        }
        if completed:
            state.put_artifact("objective_result", f"result:{self.objective_id}",
                               content=report, objective_id=self.objective_id)
        state.set_kv(f"objective.{self.objective_id}.report", report)
        state.emit("core.finished", "core",
                   {"completed": completed, "failed": len(failed)}, self.objective_id)
        return report

    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        return {"objective_id": self.objective_id,
                "objective": state.get_kv(f"objective.{self.objective_id}.text"),
                "graph": self.graph.progress(),
                "agents": self.agents.status(),
                "artifacts": len(state.artifacts(self.objective_id))}

    @staticmethod
    def current() -> Optional[str]:
        return state.get_kv("current_objective")


def core(objective_id: Optional[str] = None) -> UnifiedCore:
    return UnifiedCore(objective_id)
