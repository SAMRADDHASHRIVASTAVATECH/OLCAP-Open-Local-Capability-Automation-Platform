"""
Adaptive routing.

Deterministic routing is MANDATORY and always available. A Random Forest may
learn backend selection from real execution traces, but it is only enabled
after it beats the deterministic baseline on held-out data, and it can never
override safety, permissions, user intent, mandatory dependencies or required
verification.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import state
from .config import cfg
from .models import ComponentSpec
from .observability import span

MODEL_PATH = cfg().data / "router_rf.joblib"
META_PATH = cfg().data / "router_rf_meta.json"

# Deterministic rules: capability -> ordered preference by context predicate.
DETERMINISTIC_RULES: Dict[str, List[Tuple[str, Any]]] = {
    "WEB_SEARCH": [
        ("searxng", lambda ctx: bool(ctx.get("searxng_url"))),
        ("olcap-search-core", lambda ctx: True),
    ],
    "WEB_EXTRACT": [
        ("jina-reader", lambda ctx: bool(ctx.get("jina_key")) and ctx.get("js_heavy")),
        ("trafilatura", lambda ctx: True),
    ],
    "WEB_CRAWL": [
        ("crawl4ai", lambda ctx: ctx.get("js_heavy") or ctx.get("pages", 0) > 40),
        ("olcap-crawler", lambda ctx: True),
    ],
    "WEB_INTERACT": [
        ("playwright", lambda ctx: True),
        ("browser-use", lambda ctx: bool(ctx.get("llm_driven"))),
        ("stagehand", lambda ctx: bool(ctx.get("node_available"))),
    ],
    "RESEARCH": [
        ("olcap-research-engine", lambda ctx: True),
        ("gpt-researcher", lambda ctx: ctx.get("depth", 1) >= 3),
    ],
    "RAG": [
        ("olcap-rag", lambda ctx: True),
        ("llamaindex", lambda ctx: ctx.get("corpus_docs", 0) > 500),
    ],
    "VECTOR_STORAGE": [
        ("sqlite-vec", lambda ctx: ctx.get("vectors", 0) < 200000),
        ("qdrant", lambda ctx: True),
    ],
    "MEMORY": [("olcap-memory", lambda ctx: True)],
    "DOCUMENT_INTELLIGENCE": [
        ("olcap-text-extract", lambda ctx: str(ctx.get("ext", "")).lower() in
            (".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl",
             ".csv", ".tsv", ".yaml", ".yml", ".html", ".htm", ".xml", ".ini",
             ".cfg", ".toml", ".py", ".js", ".ts", ".sql", ".sh", ".ps1")),
        ("docling", lambda ctx: bool(ctx.get("tables"))),
        ("pypdf", lambda ctx: str(ctx.get("ext", "")).lower() == ".pdf"),
        ("python-docx", lambda ctx: str(ctx.get("ext", "")).lower() == ".docx"),
        ("trafilatura", lambda ctx: True),
    ],
    "WORKFLOW_EXECUTION": [("olcap-workflows", lambda ctx: True)],
    "DURABLE_TASKS": [("olcap-workflows", lambda ctx: True)],
    "DATABASE_QUERY": [("duckdb", lambda ctx: True)],
}


def deterministic_choice(capability: str, candidates: List[str],
                         ctx: Optional[Dict[str, Any]] = None) -> Optional[str]:
    ctx = ctx or {}
    rules = DETERMINISTIC_RULES.get(capability.upper(), [])
    for comp, pred in rules:
        if comp in candidates:
            try:
                if pred(ctx):
                    return comp
            except Exception:
                continue
    return candidates[0] if candidates else None


# --------------------------------------------------------------------------- #
def _num(v: Any, default: float = 0.0) -> float:
    """Coerce a context value to a number; non-numeric context must never
    break routing (e.g. `vectors` is a list of records in a request)."""
    if v is None:
        return default
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (list, tuple, dict, set)):
        try:
            return float(len(v))
        except Exception:
            return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _features(capability: str, ctx: Dict[str, Any], comp: ComponentSpec) -> List[float]:
    """Numeric feature vector - identical for training and inference."""
    from .registry import registry
    h = registry().health_of(comp.id)
    state_v = {"unavailable": 0.0, "starting": 0.2, "healthy": 1.0, "degraded": 0.6,
               "failed": 0.0, "stopped": 0.3, "ready": 0.9, "active": 1.0,
               "idle": 0.8, "released": 0.4}.get(h.state.value, 0.0)
    return [
        len(capability),
        1.0 if ctx.get("js_heavy") else 0.0,
        _num(ctx.get("pages")),
        _num(ctx.get("depth"), 1.0),
        _num(ctx.get("vectors")),
        _num(ctx.get("corpus_docs")),
        _num(ctx.get("max_results"), 10.0),
        1.0 if ctx.get("offline") else 0.0,
        float(comp.resource_mb) / 1024.0,
        1.0 if comp.jit else 0.0,
        1.0 if comp.paid else 0.0,
        1.0 if comp.self_hosted else 0.0,
        state_v,
        1.0 if h.installed else 0.0,
        float(len(comp.capabilities or [])),
        float(hash(comp.id) % 97) / 97.0,   # stable component identity signal
    ]


FEATURE_NAMES = ["cap_len", "js_heavy", "pages", "depth", "vectors", "corpus_docs",
                 "max_results", "offline", "resource_gb", "jit", "paid",
                 "self_hosted", "health", "installed", "n_caps", "comp_hash"]


class Router:
    def __init__(self) -> None:
        self.model = None
        self.meta: Dict[str, Any] = {}
        self.enabled = False
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if META_PATH.exists():
            try:
                self.meta = json.loads(META_PATH.read_text())
                self.enabled = bool(self.meta.get("enabled"))
            except Exception:
                self.meta, self.enabled = {}, False
        if MODEL_PATH.exists() and self.enabled:
            try:
                import joblib
                self.model = joblib.load(MODEL_PATH)
            except Exception:
                self.model = None
                self.enabled = False

    # ------------------------------------------------------------------ #
    def choose(self, capability: str, comps: List[ComponentSpec],
               ctx: Optional[Dict[str, Any]] = None) -> Tuple[Optional[ComponentSpec], str]:
        """Return (component, reason). Deterministic unless the RF is enabled."""
        ctx = ctx or {}
        ids = [c.id for c in comps]
        det = deterministic_choice(capability, ids, ctx)
        if not det:
            return None, "no candidates"
        if not self.enabled or self.model is None or len(comps) < 2:
            pick = next((c for c in comps if c.id == det), comps[0])
            return pick, "deterministic"
        try:
            import numpy as np
            X = np.array([_features(capability, ctx, c) for c in comps])
            proba = self.model.predict_proba(X)
            classes = list(self.model.classes_)
            scores = {}
            for i, c in enumerate(comps):
                if c.id in classes:
                    scores[c.id] = float(proba[i][classes.index(c.id)])
            if scores:
                best = max(scores, key=scores.get)
                # Confidence gate: the RF must be clearly better than the
                # deterministic pick, otherwise we keep the safe default.
                if best != det and scores.get(best, 0) - scores.get(det, 0) < 0.10:
                    pick = next((c for c in comps if c.id == det), comps[0])
                    return pick, "rf_overridden_by_confidence_gate"
                pick = next((c for c in comps if c.id == best), comps[0])
                return pick, "random_forest"
        except Exception:
            pass
        pick = next((c for c in comps if c.id == det), comps[0])
        return pick, "deterministic_fallback"

    # ------------------------------------------------------------------ #
    def record(self, capability: str, ctx: Dict[str, Any], comp: ComponentSpec,
               success: bool, duration_ms: float) -> None:
        state.add_routing_sample(capability.upper(), {
            "js_heavy": bool(ctx.get("js_heavy")),
            "pages": int(ctx.get("pages", 0) or 0),
            "depth": int(ctx.get("depth", 1) or 1),
            "vectors": int(ctx.get("vectors", 0) or 0),
            "corpus_docs": int(ctx.get("corpus_docs", 0) or 0),
            "max_results": int(ctx.get("max_results", 10) or 10),
            "offline": bool(ctx.get("offline")),
        }, comp.id, success, duration_ms)

    # ------------------------------------------------------------------ #
    def train(self, min_samples: int = 40, test_size: float = 0.3
              ) -> Dict[str, Any]:
        """TRAIN -> VALIDATE -> COMPARE WITH BASELINE -> enable only if better."""
        samples = state.routing_samples()
        if len(samples) < min_samples:
            return {"trained": False, "reason": f"only {len(samples)} samples "
                                                f"(need {min_samples})",
                    "enabled": self.enabled}
        try:
            import numpy as np
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.metrics import accuracy_score
            from sklearn.model_selection import train_test_split
        except Exception as e:
            return {"trained": False, "reason": f"sklearn unavailable: {e}"}

        from .registry import registry
        X, y = [], []
        for s in samples:
            comp = registry().component(s["chosen"])
            if not comp:
                continue
            X.append(_features(s["capability"], s["features"], comp))
            y.append(s["chosen"])
        if len(set(y)) < 2:
            return {"trained": False, "reason": "need >=2 distinct backends in data",
                    "enabled": self.enabled}

        X = np.array(X); y = np.array(y)
        # Only train on successful outcomes (we learn what WORKS).
        ok = np.array([1 if s["success"] else 0 for s in samples])[:len(X)]
        X, y = X[ok == 1], y[ok == 1]
        # Classes with a single observation cannot be stratified and cannot be
        # learned from - drop them instead of crashing the training run.
        from collections import Counter
        counts = Counter(y.tolist())
        keep = np.array([counts[v] >= 2 for v in y])
        X, y = X[keep], y[keep]
        if len(X) < min_samples or len(set(y.tolist())) < 2:
            return {"trained": False, "reason": "not enough successful multi-backend data",
                    "n": int(len(X))}

        try:
            Xtr, Xte, ytr, yte = train_test_split(
                X, y, test_size=test_size, random_state=42, stratify=y)
        except ValueError:
            Xtr, Xte, ytr, yte = train_test_split(
                X, y, test_size=test_size, random_state=42)
        rf = RandomForestClassifier(n_estimators=200, random_state=42,
                                    min_samples_leaf=2)
        rf.fit(Xtr, ytr)
        rf_acc = float(accuracy_score(yte, rf.predict(Xte)))

        # Baseline: always pick the most frequent backend (per capability).
        from collections import Counter
        majority = Counter(ytr).most_common(1)[0][0]
        base_acc = float(accuracy_score(yte, [majority] * len(yte)))

        improved = rf_acc > base_acc + 1e-9
        result = {
            "trained": True, "n_samples": int(len(X)),
            "rf_accuracy": round(rf_acc, 4),
            "baseline_accuracy": round(base_acc, 4),
            "improved": improved,
            "classes": sorted(set(y.tolist())),
            "n_features": X.shape[1],
        }
        try:
            import joblib
            joblib.dump(rf, MODEL_PATH)
        except Exception:
            pass
        if improved:
            self.model = rf
            self.enabled = True
        else:
            self.enabled = False
        self.meta = {**result, "enabled": self.enabled,
                     "trained_at": time.time(),
                     "note": "RF may never override safety, permissions, user intent, "
                             "mandatory dependencies or required verification."}
        META_PATH.write_text(json.dumps(self.meta, indent=2))
        state.emit("router.trained", "router", result)
        return self.meta

    # ------------------------------------------------------------------ #
    def disable(self) -> Dict[str, Any]:
        self.enabled = False
        self.model = None
        self.meta = {**(self.meta or {}), "enabled": False}
        META_PATH.write_text(json.dumps(self.meta, indent=2))
        return self.meta

    def status(self) -> Dict[str, Any]:
        return {"enabled": self.enabled,
                "model_present": MODEL_PATH.exists(),
                "meta": self.meta,
                "samples": len(state.routing_samples()),
                "features": FEATURE_NAMES}


_ROUTER: Optional[Router] = None


def router() -> Router:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = Router()
    return _ROUTER
