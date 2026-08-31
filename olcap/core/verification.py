"""
Verification: prove the outcome, not the intention.

Every important result gets checked against explicit success criteria. A result
that cannot be verified is reported as unverified - never as success.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import state
from .models import NodeStatus
from .observability import span


def verify_artifact(params: Dict[str, Any]) -> Dict[str, Any]:
    aid = params.get("artifact_id") or ""
    criteria = params.get("criteria") or []
    result = params.get("result") or {}

    if aid:
        art = state.artifact(aid)
        if not art:
            return {"ok": False, "passed": False,
                    "error": f"artifact {aid} not found"}
        try:
            result = json.loads(art["content"]) if art.get("content") else {}
        except Exception:
            result = {"content": art.get("content", "")}
        path = art.get("path") or ""

    checks: List[Dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str = ""):
        checks.append({"check": name, "passed": bool(passed), "detail": str(detail)[:300]})

    # Generic structural checks
    if isinstance(result, dict):
        add("result_non_empty", bool(result), f"keys={len(result)}")
        if "error" in result:
            add("no_error_field", False, str(result.get("error"))[:200])
    else:
        add("result_non_empty", bool(str(result).strip()))

    # Explicit criteria: each is either a dict {name, kind, expect} or a string
    for c in criteria:
        if isinstance(c, str):
            add(c, _check_text(c, result), "string criterion evaluated")
        elif isinstance(c, dict):
            name = c.get("name") or "criterion"
            kind = c.get("kind") or "expression"
            try:
                if kind == "expression":
                    # `expr` is canonical; the aliases are accepted because a
                    # criterion written by a model or a human often says
                    # `expect`/`expression`. Missing -> explicit failure, never
                    # a silent KeyError passed off as "criterion not met".
                    src = (c.get("expr") or c.get("expect")
                           or c.get("expression") or "")
                    if not src:
                        add(name, False,
                            "expression criterion has no expr/expect/expression")
                    else:
                        safe = {"result": result, "ok": result.get("ok", True)
                                if isinstance(result, dict) else True,
                                "len": len, "str": str, "sum": sum, "any": any,
                                "all": all, "abs": abs}
                        passed = bool(eval(src, {"__builtins__": {}}, safe))  # noqa: S307
                        add(name, passed, src)
                elif kind == "contains":
                    blob = json.dumps(result, default=str)
                    add(name, str(c.get("value")) in blob, f"contains {c.get('value')}")
                elif kind == "min_length":
                    blob = json.dumps(result, default=str)
                    add(name, len(blob) >= int(c.get("value", 1)),
                        f"len={len(blob)} >= {c.get('value')}")
                elif kind == "file_exists":
                    add(name, Path(str(c.get("path", ""))).exists(), c.get("path", ""))
                elif kind == "min_sources":
                    n = len((result.get("sources") if isinstance(result, dict) else []) or [])
                    add(name, n >= int(c.get("value", 1)), f"sources={n}")
                elif kind == "min_confidence":
                    v = float((result or {}).get(c.get("field", "confidence"), 0) or 0)
                    add(name, v >= float(c.get("value", 0.5)), f"{v} >= {c.get('value')}")
                elif kind == "relevance":
                    # Does the output actually talk about the objective?
                    # Guards against unrelated work passing as a success when
                    # the criteria are generic.
                    objective = str(c.get("value") or "")
                    blob = json.dumps(result, default=str).lower()
                    terms = _distinctive_terms(objective)
                    if not terms:
                        add(name, True, "no distinctive terms - not applicable")
                    else:
                        hits = [t for t in terms if t in blob]
                        missing = [t for t in terms if t not in blob]
                        frac = len(hits) / len(terms)
                        floor = 2 if len(terms) >= 3 else 1
                        add(name, frac >= 0.34 and len(hits) >= floor,
                            f"matched {len(hits)}/{len(terms)} objective terms "
                            f"({frac:.0%}); missing={missing[:6]}")
                else:
                    add(name, False, f"unknown criterion kind {kind}")
            except Exception as e:
                add(name, False, f"{type(e).__name__}: {e}")

    passed = all(c["passed"] for c in checks) if checks else False
    out = {"ok": True, "passed": passed, "checks": checks,
           "artifact_id": aid, "criteria_count": len(criteria)}
    state.emit("verification.done", "verification",
               {"passed": passed, "checks": len(checks)})
    return out


# Function words and generic task verbs: they carry no information about
# *what* an objective is about, so relevance is judged on the rest.
_STOP_TERMS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "about",
    "then", "than", "into", "will", "shall", "should", "would", "could",
    "have", "has", "had", "been", "being", "are", "was", "were", "also",
    "more", "most", "some", "any", "all", "not", "but", "its", "you",
    "your", "our", "their", "them", "they", "what", "when", "where",
    "which", "while", "after", "before", "over", "under", "again",
    "please", "make", "create", "write", "produce", "generate", "give",
    "tell", "show", "need", "want", "must", "does", "done", "task",
    "objective", "goal", "using", "used", "uses", "new", "one", "two",
}


def _distinctive_terms(text: str) -> List[str]:
    """Content words (>=4 chars, not function words) that identify a topic."""
    words = re.findall(r"[a-z0-9][a-z0-9\-']{3,}", (text or "").lower())
    out: List[str] = []
    for w in words:
        w = w.strip("-'")
        if len(w) < 4 or w in _STOP_TERMS or w.isdigit():
            continue
        if w not in out:
            out.append(w)
    return out


def _check_text(criterion: str, result: Any) -> bool:
    blob = json.dumps(result, default=str).lower()
    terms = [t for t in re.findall(r"[a-z0-9]{4,}", criterion.lower())]
    return sum(1 for t in terms if t in blob) >= max(1, len(terms) // 2)


def verify_capability_output(capability: str, result: Dict[str, Any],
                             params: Dict[str, Any]) -> Dict[str, Any]:
    """Automatic sanity checks per capability family."""
    with span("verification.capability", "verification", {"capability": capability}):
        if capability == "WEB_SEARCH":
            n = len(result.get("results", []) or [])
            return {"passed": n > 0, "detail": f"{n} results", "checks": [
                {"check": "has_results", "passed": n > 0},
                {"check": "all_have_url",
                 "passed": all(r.get("url") for r in result.get("results", []))}]}
        if capability in ("WEB_EXTRACT", "DOCUMENT_INTELLIGENCE"):
            t = (result.get("text") or "")
            return {"passed": len(t.strip()) > 40,
                    "detail": f"{len(t)} chars", "checks": [
                        {"check": "non_empty_text", "passed": len(t.strip()) > 40}]}
        if capability == "WEB_CRAWL":
            n = len(result.get("pages", []) or [])
            return {"passed": n > 0, "detail": f"{n} pages", "checks": [
                {"check": "pages_returned", "passed": n > 0}]}
        if capability == "RESEARCH":
            src = len(result.get("sources", []) or [])
            rep = len(result.get("report", "") or "")
            return {"passed": src > 0 and rep > 80,
                    "detail": f"{src} sources / {rep} chars", "checks": [
                        {"check": "has_sources", "passed": src > 0},
                        {"check": "has_report", "passed": rep > 80}]}
        if capability == "RAG":
            grounded = bool(result.get("grounded"))
            return {"passed": grounded, "detail": f"grounded={grounded}",
                    "checks": [{"check": "grounded", "passed": grounded}]}
        if capability in ("DATA_ANALYSIS", "DATABASE_QUERY"):
            has = bool(result.get("rows") or result.get("columns") or result.get("summary"))
            return {"passed": has, "detail": "rows/columns present",
                    "checks": [{"check": "has_result_set", "passed": has}]}
        return {"passed": True, "detail": "no automatic check defined", "checks": []}


def verify_node(node_id: str, criteria: List[Any], result: Dict[str, Any]
                ) -> Dict[str, Any]:
    out = verify_artifact({"criteria": criteria, "result": result})
    state.emit("verification.node", "verification",
               {"node": node_id, "passed": out["passed"]})
    return out
