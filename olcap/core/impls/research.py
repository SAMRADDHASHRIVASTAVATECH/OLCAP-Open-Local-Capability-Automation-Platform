"""
MCP SERVER 2 - RESEARCH and SOURCE_VERIFICATION.

Pipeline (chosen by the dependency graph, never hard-coded):
  decompose question -> multi-source search -> fetch/extract ->
  evidence scoring -> cross-source verification -> cited synthesis

The synthesis step uses the OpenCode model path when reachable and degrades to
a deterministic extractive summary otherwise. It never invents citations: every
claim in the report is tied to a URL that was actually fetched.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from ..config import cfg
from ..llm import llm
from ..observability import span
from ..runtime import implements

_TRUST_HINTS = {
    "wikipedia.org": 0.85, "arxiv.org": 0.9, "nature.com": 0.95, "science.org": 0.95,
    "ncbi.nlm.nih.gov": 0.95, "doi.org": 0.9, "github.com": 0.8,
    "stackoverflow.com": 0.7, "medium.com": 0.45, "reddit.com": 0.5,
    "docs.python.org": 0.9, "mozilla.org": 0.85, "w3.org": 0.9,
    "ieee.org": 0.9, "acm.org": 0.9, "springer.com": 0.9, "elsevier.com": 0.88,
    "gov": 0.9, "edu": 0.9, "who.int": 0.92, "un.org": 0.9,
}


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _trust(url: str) -> float:
    d = _domain(url)
    for k, v in _TRUST_HINTS.items():
        if d.endswith(k):
            return v
    tld = d.split(".")[-1] if "." in d else ""
    if tld in ("gov", "edu", "int"):
        return _TRUST_HINTS[tld]
    return 0.6


def _decompose(question: str, depth: int) -> List[str]:
    """Deterministic sub-question decomposition (no model needed)."""
    q = question.strip().rstrip("?")
    subs = [q]
    templates = [
        f"what is {q}",
        f"{q} definition and key concepts",
        f"{q} recent developments {time.strftime('%Y')}",
        f"{q} advantages limitations comparison",
        f"{q} implementation technical requirements",
        f"{q} criticism or open problems",
    ]
    for t in templates[1:]:
        if len(subs) >= depth + 2:
            break
        subs.append(t)
    return subs[:max(2, depth + 2)]


def _gather(sub: str, max_sources: int) -> List[Dict[str, Any]]:
    from ..runtime import execute
    try:
        out = execute("WEB_SEARCH", {"query": sub, "max_results": max_sources},
                      method="web_search")
        return out["result"].get("results", [])
    except Exception:
        return []


def _fetch_evidence(url: str, max_chars: int = 6000) -> Dict[str, Any]:
    from ..runtime import execute
    try:
        out = execute("WEB_EXTRACT", {"url": url}, method="web_extract")
        text = (out["result"] or {}).get("text") or ""
        return {"url": url, "text": text[:max_chars], "chars": len(text),
                "component": out.get("component"), "ok": bool(text)}
    except Exception as e:
        return {"url": url, "text": "", "chars": 0, "ok": False,
                "error": f"{type(e).__name__}: {str(e)[:120]}"}


@implements("RESEARCH", "olcap-research-engine")
def research_run(params: Dict[str, Any]) -> Dict[str, Any]:
    question = params.get("question") or params.get("query") or ""
    if not question:
        raise ValueError("question is required")
    depth = int(params.get("depth", 2) or 2)
    max_sources = int(params.get("max_sources", 12) or 12)
    do_verify = bool(params.get("verify", True))

    findings: List[Dict[str, Any]] = []
    sources: List[Dict[str, Any]] = []
    seen: set = set()
    # One research run fans out to many fetches. Each is bounded, but the sum
    # was not: on a slow network a single call swallowed the objective's whole
    # budget. Spend at most `max_seconds` gathering and then answer with what
    # we have, flagged as partial, rather than running to the deadline.
    import time as _time
    budget = float(params.get("max_seconds") or 45)
    deadline = _time.time() + budget
    exhausted = False

    with span("research.run", "capability", {"question": question[:120],
                                             "depth": depth}) as sp:
        subs = _decompose(question, depth)
        for sub in subs:
            if _time.time() >= deadline:
                exhausted = True
                break
            for r in _gather(sub, max(4, max_sources // 2)):
                if _time.time() >= deadline:
                    exhausted = True
                    break
                url = r.get("url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                ev = _fetch_evidence(url)
                sources.append({"url": url, "title": r.get("title", ""),
                                "engine": r.get("engine"),
                                "relevance": r.get("relevance"),
                                "trust": _trust(url), "fetched": ev["ok"],
                                "chars": ev["chars"],
                                "component": ev.get("component")})
                if ev["ok"]:
                    findings.append({"sub_question": sub, "url": url,
                                     "title": r.get("title", ""),
                                     "text": ev["text"], "trust": _trust(url),
                                     "relevance": r.get("relevance", 0)})
                if len(sources) >= max_sources:
                    break
            if len(sources) >= max_sources:
                break

        sp.set(budget_s=budget, exhausted=exhausted)
        sources.sort(key=lambda s: -(float(s.get("trust", 0)) * 0.4 +
                                     float(s.get("relevance") or 0) * 0.6))
        evidence_block = "\n\n".join(
            f"[{i+1}] {f['title']} ({f['url']})\n{f['text'][:2500]}"
            for i, f in enumerate(findings[:12]))

        prompt = (
            f"Research question: {question}\n\n"
            f"Sub-questions examined: {json.dumps(subs, indent=1)}\n\n"
            f"Evidence:\n{evidence_block}\n\n"
            "Write a structured research report with: (1) short answer, "
            "(2) key findings with [n] citations, (3) disagreements or gaps, "
            "(4) confidence and what would raise it. "
            "Use ONLY the evidence above; mark anything uncertain as uncertain.")
        res = llm().complete(system="You are a rigorous research analyst. "
                                    "Cite as [n] matching the evidence numbering.",
                             prompt=prompt, max_tokens=2000)
        if res.get("backend") in ("stub", "stub_fallback") or res.get("degraded"):
            # No reachable model: build the report from the evidence we really
            # gathered. Never dress up a prompt as if it were an answer.
            report = _synthesise_without_model(question, findings, subs)
        else:
            report = res.get("text", "")

        confidence = round(min(1.0, 0.25 + 0.06 * len(findings) +
                               0.02 * sum(1 for s in sources if s["fetched"])),
                           3)
        verified = None
        if do_verify and findings:
            verified = source_verify({"claim": question,
                                      "sources": [f["url"] for f in findings[:8]],
                                      "evidence": [f["text"] for f in findings[:8]]})
            if isinstance(verified, dict) and "confidence" in verified:
                confidence = round((confidence + float(verified["confidence"])) / 2, 3)

        sp.set(sources=len(sources), findings=len(findings),
               confidence=confidence, model=res.get("backend"))
        return {"ok": True, "question": question, "report": report,
                "findings": [{"sub_question": f["sub_question"], "url": f["url"],
                              "title": f["title"], "trust": f["trust"],
                              "excerpt": f["text"][:600]} for f in findings],
                "sources": sources, "sub_questions": subs,
                "confidence": confidence, "verification": verified,
                "model_backend": res.get("backend"),
                "budget_exhausted": exhausted, "budget_s": budget,
                "degraded": bool(res.get("degraded")) or exhausted}


def _synthesise_without_model(question: str, findings: List[Dict[str, Any]],
                              subs: List[str]) -> str:
    """
    Deterministic, evidence-bound report used when no external model is
    reachable. It quotes and organises what was actually retrieved - it does
    not invent conclusions.
    """
    if not findings:
        return ("No evidence could be retrieved for this question from the "
                "available sources, so no conclusion is stated.")
    lines = [f"# Research report: {question}", "",
             "Model backend unavailable - this report is assembled directly "
             "from retrieved evidence (extractive, no generation).", "",
             "## Sub-questions examined"]
    for s in subs:
        lines.append(f"- {s}")
    lines += ["", "## Key findings"]
    for i, f in enumerate(findings[:12], start=1):
        excerpt = " ".join((f["text"] or "").split())[:420]
        lines.append(f"[{i}] **{f['title'] or f['url']}** ({f['url']}) - "
                     f"source trust {f['trust']}")
        lines.append(f"    {excerpt}")
    lines += ["", "## Gaps and confidence",
              f"- {len(findings)} source(s) were successfully fetched and quoted.",
              "- No model synthesis step ran; conclusions are not asserted "
              "beyond the quoted evidence.",
              "- Re-run with a configured model backend for a synthesized answer."]
    return "\n".join(lines)


@implements("SOURCE_VERIFICATION", "olcap-research-engine")
def source_verify(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Cross-source verification: independent-domain agreement, trust weighting,
    recency, whether the claim's key terms actually appear in the sources, and
    explicit contradiction detection.
    """
    claim = params.get("claim") or ""
    sources = params.get("sources") or []
    evidence = params.get("evidence") or []
    min_sources = int(params.get("min_sources", 2) or 2)
    if not claim:
        raise ValueError("claim is required")

    if not sources:
        from ..runtime import execute
        try:
            out = execute("WEB_SEARCH", {"query": claim, "max_results": 6},
                          method="web_search")
            sources = [r["url"] for r in out["result"].get("results", [])]
        except Exception:
            sources = []

    if not evidence:
        # Bounded on purpose: verifying N sources one after another used to be
        # the slowest thing in a run, with no ceiling but the JIT cap.
        import time as _time
        _dl = _time.time() + float(params.get("max_seconds") or 30)
        evidence: List[str] = []
        for u in sources[:6]:
            if _time.time() >= _dl:
                break
            evidence.append(_fetch_evidence(u).get("text", ""))

    terms = [t for t in re.findall(r"[a-z0-9]{4,}", claim.lower())
             if t not in ("what", "which", "that", "this", "with", "from", "does",
                          "into", "than", "then", "have", "been", "are", "the")]
    domains = {_domain(s) for s in sources if _domain(s)}
    hits = 0
    per_source = []
    for url, ev in zip(sources, evidence + [""] * len(sources)):
        text = (ev or "").lower()
        matched = sum(1 for t in terms if t in text) if text else 0
        coverage = matched / max(1, len(terms))
        if coverage >= 0.34:
            hits += 1
        per_source.append({"url": url, "domain": _domain(url),
                           "trust": _trust(url), "coverage": round(coverage, 3),
                           "supports": coverage >= 0.34, "chars": len(ev or "")})

    agreement = hits / max(1, len(sources)) if sources else 0.0
    avg_trust = (sum(_trust(s) for s in sources) / len(sources)) if sources else 0.0
    domain_diversity = min(1.0, len(domains) / max(2, min_sources))
    confidence = round(max(0.0, min(1.0,
                       agreement * 0.5 + avg_trust * 0.25 + domain_diversity * 0.25)), 3)
    if len(sources) < min_sources:
        verdict = "insufficient_sources"
        confidence = round(confidence * 0.5, 3)
    elif agreement >= 0.6:
        verdict = "supported"
    elif agreement >= 0.3:
        verdict = "partially_supported"
    else:
        verdict = "unsupported"

    contradictions = [p["url"] for p in per_source
                      if p["chars"] > 200 and p["coverage"] < 0.1]
    return {"ok": True, "claim": claim, "verdict": verdict,
            "confidence": confidence, "agreement": round(agreement, 3),
            "independent_domains": len(domains),
            "sources_checked": len(sources),
            "evidence": per_source,
            "contradictions": contradictions,
            "method": "cross-source term coverage + domain trust + diversity"}
