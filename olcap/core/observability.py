"""
Observability: OpenTelemetry-compatible spans persisted locally (no cloud
dependency). Records model calls, agent calls, skill use, MCP calls, capability
requests, JIT activations, worker lifecycle, tool calls, dependency transitions,
failures, retries, timings, resource usage, verification and routing decisions.

Secrets are redacted before anything is written.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from . import state
from .config import cfg

_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|token|secret|password|passwd|"
    r"[gs]k-[A-Za-z0-9_\-]{8,})[\"'\s:=]+([A-Za-z0-9_\-\.]{6,})")


def redact(obj: Any, truncate: bool = True) -> Any:
    """Redact secret-looking values.

    `truncate` caps the result for log lines and trace attributes; the state
    store passes truncate=False because silently chopping a stored value at
    4000 characters is data loss, not redaction.
    """
    try:
        s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    except Exception:
        return obj
    s = _SECRET_RE.sub(lambda m: f"{m.group(1)}=***REDACTED***", s)
    if truncate and len(s) > 4000:
        s = s[:4000] + "...[truncated]"
    if isinstance(obj, str):
        return s
    try:
        return json.loads(s)
    except Exception:
        return s


class Span:
    def __init__(self, name: str, kind: str = "internal", trace_id: Optional[str] = None,
                 parent_id: Optional[str] = None, attributes: Optional[Dict[str, Any]] = None):
        self.id = uuid.uuid4().hex[:16]
        self.trace_id = trace_id or uuid.uuid4().hex[:24]
        self.parent_id = parent_id
        self.name = name
        self.kind = kind
        self.start = time.time()
        self.end: Optional[float] = None
        self.status = "running"
        self.attributes: Dict[str, Any] = dict(attributes or {})
        self.children: List["Span"] = []

    def set(self, **kw) -> "Span":
        self.attributes.update(redact(kw) if False else {k: redact(v) for k, v in kw.items()})
        return self

    def finish(self, status: str = "ok", **kw) -> "Span":
        if self.end is None:
            self.end = time.time()
            self.status = status
            if kw:
                self.set(**kw)
            _persist(self)
        return self

    @property
    def duration_ms(self) -> float:
        return (self.end or time.time()) - self.start and \
            round(((self.end or time.time()) - self.start) * 1000, 2)


_current: Dict[int, Span] = {}
_root: Optional[Span] = None


def _persist(s: Span) -> None:
    try:
        c = state.conn()
        c.execute("""INSERT INTO spans(id,trace_id,parent_id,name,kind,start,end,duration_ms,
                     status,attributes) VALUES(?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET end=excluded.end,
                     duration_ms=excluded.duration_ms, status=excluded.status,
                     attributes=excluded.attributes""",
                  (s.id, s.trace_id, s.parent_id, s.name, s.kind, s.start, s.end or time.time(),
                   s.duration_ms, s.status, json.dumps(s.attributes, default=str)[:8000]))
        c.commit()
    except Exception:
        pass
    try:
        line = json.dumps({"ts": s.start, "span": s.name, "kind": s.kind,
                           "status": s.status, "dur_ms": s.duration_ms,
                           "attrs": s.attributes}, default=str)
        with (cfg().traces / f"trace-{time.strftime('%Y%m%d')}.jsonl").open("a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


@contextmanager
def span(name: str, kind: str = "internal",
         attributes: Optional[Dict[str, Any]] = None) -> Iterator[Span]:
    import threading
    tid = threading.get_ident()
    parent = _current.get(tid)
    s = Span(name, kind, trace_id=(parent.trace_id if parent else None),
             parent_id=(parent.id if parent else None), attributes=attributes)
    _current[tid] = s
    try:
        yield s
        s.finish("ok")
    except Exception as e:
        s.finish("error", error=f"{type(e).__name__}: {e}")
        raise
    finally:
        _current[tid] = parent


def log(kind: str, actor: str = "", **payload) -> None:
    state.emit(kind, actor, redact(payload))


def counters(window: int = 5000) -> Dict[str, Any]:
    c = state.conn()
    rows = c.execute("""SELECT kind, status, COUNT(*) n, AVG(duration_ms) avg_ms,
                        MAX(duration_ms) max_ms FROM spans GROUP BY kind, status""").fetchall()
    return {f"{r['kind']}.{r['status']}": {"count": r["n"],
                                           "avg_ms": round(r["avg_ms"] or 0, 2),
                                           "max_ms": round(r["max_ms"] or 0, 2)}
            for r in rows}


def recent_spans(limit: int = 100) -> List[Dict[str, Any]]:
    rows = state.conn().execute(
        "SELECT * FROM spans ORDER BY start DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
