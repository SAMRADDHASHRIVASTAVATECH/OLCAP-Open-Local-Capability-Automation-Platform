"""
ONE AUTHORITATIVE STATE.

Every layer (Unified Core, Dependency Graph, Capability Registry, agents,
all three MCP servers, JIT workers, workflows) reads and writes through this
module. There is no second authoritative store: caches are derived and
rebuildable, and every mutation is journalled so task state can never be lost.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import cfg
from .models import GraphEdge, GraphNode, NodeStatus

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=8000;

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    objective_id TEXT,
    type TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL,
    capability TEXT,
    component TEXT,
    agent TEXT,
    payload TEXT,
    result TEXT,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    critical INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_nodes_obj ON nodes(objective_id);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    type TEXT NOT NULL,
    optional INTEGER DEFAULT 0,
    weight REAL DEFAULT 1.0,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    objective_id TEXT,
    kind TEXT NOT NULL,
    actor TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    objective_id TEXT,
    node_id TEXT,
    kind TEXT,
    name TEXT,
    path TEXT,
    content TEXT,
    meta TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    trace_id TEXT,
    parent_id TEXT,
    name TEXT NOT NULL,
    kind TEXT,
    start REAL,
    end REAL,
    duration_ms REAL,
    status TEXT,
    attributes TEXT
);

CREATE TABLE IF NOT EXISTS routing_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    capability TEXT,
    features TEXT,
    chosen TEXT,
    success INTEGER,
    duration_ms REAL
);

CREATE TABLE IF NOT EXISTS components (
    id TEXT PRIMARY KEY,
    state TEXT,
    installed INTEGER DEFAULT 0,
    configured INTEGER DEFAULT 0,
    version TEXT,
    health TEXT,
    updated_at REAL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    ts REAL,
    action TEXT,
    category TEXT,
    resource TEXT,
    decision TEXT,
    reason TEXT,
    auto INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS memory (
    id TEXT PRIMARY KEY,
    kind TEXT,
    text TEXT,
    meta TEXT,
    salience REAL DEFAULT 0.5,
    created_at REAL,
    last_access REAL,
    access_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory(kind);
"""

_lock = threading.RLock()
_db_path: Optional[Path] = None

# One connection PER THREAD. A single shared connection looks safe with
# check_same_thread=False but sqlite3 raises "bad parameter or other API
# misuse" as soon as two threads touch it concurrently (parallel workflow
# steps, parallel agents, parallel MCP calls). WAL + busy_timeout makes the
# per-thread connections safe against each other.
_local = threading.local()


def db_path() -> Path:
    return cfg().state_db


def _connect(p: Path) -> sqlite3.Connection:
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=30.0, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.executescript(_SCHEMA)
    return c


def conn() -> sqlite3.Connection:
    global _db_path
    p = db_path()
    if getattr(_local, "path", None) != p or getattr(_local, "conn", None) is None:
        # Creating a connection runs PRAGMA/SCHEMA statements; doing that from
        # several threads at once (parallel agents) causes "database is locked".
        with _lock:
            if getattr(_local, "path", None) != p or \
                    getattr(_local, "conn", None) is None:
                if _db_path != p:
                    _db_path = p
                _local.conn = _connect(p)
                _local.path = p
    return _local.conn


def close_thread_conn() -> None:
    """Release this thread's connection (used by worker shutdown / tests)."""
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    _local.conn = None
    _local.path = None


def _tx(fn):
    with _lock:
        c = conn()
        try:
            out = fn(c)
            c.commit()
            return out
        except Exception:
            c.rollback()
            raise


# --------------------------------------------------------------------------- #
# Key/value (authoritative scalars: current objective, goal, criteria, ...)
# --------------------------------------------------------------------------- #
_SECRET_KEY_RE = None


def _scrub(value: Any) -> Any:
    """Redact secret-looking values before they are persisted.

    Telemetry already redacts; the store did not, so a canary secret written
    through set_kv/emit landed in cleartext in the database file.
    """
    global _SECRET_KEY_RE
    try:
        if _SECRET_KEY_RE is None:
            import re as _re
            _SECRET_KEY_RE = _re.compile(
                r"(?i)(api[_-]?key|authorization|bearer|token|secret|password"
                r"|passwd|private[_-]?key|credential)")
        from .observability import redact as _redact
    except Exception:
        return value
    try:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                out[k] = "***REDACTED***" if (isinstance(v, (str, int, float))
                                              and _SECRET_KEY_RE.search(str(k))) else _scrub(v)
            return out
        if isinstance(value, (list, tuple)):
            return [_scrub(v) for v in value]
        if isinstance(value, str):
            # stored data must survive intact: redact secrets, never truncate
            return _redact(value, truncate=False)
        return value
    except Exception:
        return value


def set_kv(k: str, v: Any) -> None:
    global _SECRET_KEY_RE
    _scrub(v)                      # keep the regex initialised
    if _SECRET_KEY_RE is not None and _SECRET_KEY_RE.search(str(k)):
        v = "***REDACTED***"
    v = _scrub(v)

    def _f(c):
        c.execute("INSERT INTO kv(k,v,updated_at) VALUES(?,?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                  (k, json.dumps(v, default=str), time.time()))
    _tx(_f)


def get_kv(k: str, default: Any = None) -> Any:
    c = conn()
    row = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["v"])
    except Exception:
        return row["v"]


def all_kv() -> Dict[str, Any]:
    return {r["k"]: get_kv(r["k"]) for r in conn().execute("SELECT k FROM kv")}


def delete_kv(k: str) -> None:
    def _f(c):
        c.execute("DELETE FROM kv WHERE k=?", (k,))
    _tx(_f)


# --------------------------------------------------------------------------- #
# Events  (append-only journal; also used as an in-process bus)
# --------------------------------------------------------------------------- #
_subscribers: List[Any] = []


def subscribe(fn) -> None:
    _subscribers.append(fn)


def emit(kind: str, actor: str = "", payload: Optional[Dict[str, Any]] = None,
         objective_id: Optional[str] = None) -> None:
    p = _scrub(payload or {})
    def _f(c):
        c.execute("INSERT INTO events(ts,objective_id,kind,actor,payload) VALUES(?,?,?,?,?)",
                  (time.time(), objective_id, kind, actor, json.dumps(p, default=str)))
    _tx(_f)
    for fn in list(_subscribers):
        try:
            fn(kind, actor, p)
        except Exception:
            pass


def events(limit: int = 200, kind: Optional[str] = None,
           objective_id: Optional[str] = None) -> List[Dict[str, Any]]:
    q = "SELECT * FROM events"
    args: List[Any] = []
    conds = []
    if kind:
        conds.append("kind=?"); args.append(kind)
    if objective_id:
        conds.append("objective_id=?"); args.append(objective_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    rows = conn().execute(q, args).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"])
        except Exception:
            payload = {}
        out.append({"seq": r["seq"], "ts": r["ts"], "kind": r["kind"],
                    "actor": r["actor"], "objective_id": r["objective_id"],
                    "payload": payload})
    return out


# --------------------------------------------------------------------------- #
# Graph persistence
# --------------------------------------------------------------------------- #
def _node_from_row(r) -> GraphNode:
    def j(s, default):
        if s is None:
            return default
        try:
            return json.loads(s)
        except Exception:
            return default
    return GraphNode(
        id=r["id"], type=r["type"], title=r["title"] or "", status=NodeStatus(r["status"]),
        capability=r["capability"], component=r["component"], agent=r["agent"],
        payload=j(r["payload"], {}), result=j(r["result"], {}), error=r["error"],
        attempts=r["attempts"], max_attempts=r["max_attempts"],
        critical=bool(r["critical"]), objective_id=r["objective_id"],
        created_at=r["created_at"], updated_at=r["updated_at"],
        started_at=r["started_at"], finished_at=r["finished_at"],
    )


def upsert_node(n: GraphNode) -> GraphNode:
    n.updated_at = time.time()
    def _f(c):
        c.execute("""INSERT INTO nodes(id,objective_id,type,title,status,capability,component,
                     agent,payload,result,error,attempts,max_attempts,critical,created_at,
                     updated_at,started_at,finished_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET
                       objective_id=excluded.objective_id, type=excluded.type,
                       title=excluded.title, status=excluded.status,
                       capability=excluded.capability, component=excluded.component,
                       agent=excluded.agent, payload=excluded.payload,
                       result=excluded.result, error=excluded.error,
                       attempts=excluded.attempts, max_attempts=excluded.max_attempts,
                       critical=excluded.critical, updated_at=excluded.updated_at,
                       started_at=excluded.started_at, finished_at=excluded.finished_at""",
                  (n.id, n.objective_id, n.type.value, n.title, n.status.value, n.capability,
                   n.component, n.agent, json.dumps(n.payload, default=str),
                   json.dumps(n.result, default=str), n.error, n.attempts, n.max_attempts,
                   int(n.critical), n.created_at, n.updated_at, n.started_at, n.finished_at))
    _tx(_f)
    return n


def get_node(nid: str) -> Optional[GraphNode]:
    r = conn().execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
    return _node_from_row(r) if r else None


def nodes(objective_id: Optional[str] = None) -> List[GraphNode]:
    if objective_id:
        rows = conn().execute("SELECT * FROM nodes WHERE objective_id=?", (objective_id,))
    else:
        rows = conn().execute("SELECT * FROM nodes")
    return [_node_from_row(r) for r in rows]


def delete_nodes(objective_id: str) -> int:
    def _f(c):
        cur = c.execute("DELETE FROM nodes WHERE objective_id=?", (objective_id,))
        return cur.rowcount
    return _tx(_f)


def upsert_edge(e: GraphEdge) -> GraphEdge:
    def _f(c):
        c.execute("""INSERT INTO edges(id,src,dst,type,optional,weight,meta)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(id) DO UPDATE SET type=excluded.type,
                       optional=excluded.optional, weight=excluded.weight, meta=excluded.meta""",
                  (e.id, e.src, e.dst, e.type.value, int(e.optional), e.weight,
                   json.dumps(e.meta, default=str)))
    _tx(_f)
    return e


def edges() -> List[GraphEdge]:
    rows = conn().execute("SELECT * FROM edges")
    out = []
    for r in rows:
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
        except Exception:
            meta = {}
        out.append(GraphEdge(id=r["id"], src=r["src"], dst=r["dst"],
                             type=r["type"], optional=bool(r["optional"]),
                             weight=r["weight"], meta=meta))
    return out


def delete_edges(ids: Iterable[str]) -> None:
    ids = list(ids)
    if not ids:
        return
    def _f(c):
        c.executemany("DELETE FROM edges WHERE id=?", [(i,) for i in ids])
    _tx(_f)


# --------------------------------------------------------------------------- #
# Artifacts (shared across agents / servers / capabilities)
# --------------------------------------------------------------------------- #
def put_artifact(kind: str, name: str, content: Any = None, path: str = "",
                 objective_id: str = "", node_id: str = "",
                 meta: Optional[Dict[str, Any]] = None) -> str:
    from .models import new_id
    aid = new_id("art")
    if content is not None and not isinstance(content, str):
        content = json.dumps(_scrub(content), default=str)
    def _f(c):
        c.execute("""INSERT INTO artifacts(id,objective_id,node_id,kind,name,path,content,meta,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?)""",
                  (aid, objective_id, node_id, kind, name, path, content,
                   json.dumps(meta or {}, default=str), time.time()))
    _tx(_f)
    emit("artifact.created", "state", {"artifact_id": aid, "kind": kind, "name": name},
         objective_id)
    return aid


def artifact(aid: str) -> Optional[Dict[str, Any]]:
    r = conn().execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


def artifacts(objective_id: Optional[str] = None, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    q = "SELECT * FROM artifacts"
    conds, args = [], []
    if objective_id:
        conds.append("objective_id=?"); args.append(objective_id)
    if kind:
        conds.append("kind=?"); args.append(kind)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY created_at DESC"
    return [dict(r) for r in conn().execute(q, args)]


# --------------------------------------------------------------------------- #
# Component state (registry runtime view)
# --------------------------------------------------------------------------- #
def set_component(cid: str, **fields) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    def _f(c):
        c.execute("INSERT INTO components(id) VALUES(?) ON CONFLICT(id) DO NOTHING", (cid,))
        assignments = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE components SET {assignments} WHERE id=?",
                  tuple(list(fields.values()) + [cid]))
    _tx(_f)


def component(cid: str) -> Optional[Dict[str, Any]]:
    r = conn().execute("SELECT * FROM components WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def components() -> Dict[str, Dict[str, Any]]:
    return {r["id"]: dict(r) for r in conn().execute("SELECT * FROM components")}


# --------------------------------------------------------------------------- #
# Routing samples (Random Forest training data)
# --------------------------------------------------------------------------- #
def add_routing_sample(capability: str, features: Dict[str, Any], chosen: str,
                       success: bool, duration_ms: float) -> None:
    def _f(c):
        c.execute("""INSERT INTO routing_samples(ts,capability,features,chosen,success,duration_ms)
                     VALUES(?,?,?,?,?,?)""",
                  (time.time(), capability, json.dumps(features, default=str), chosen,
                   int(bool(success)), float(duration_ms)))
    _tx(_f)


def routing_samples(limit: int = 5000) -> List[Dict[str, Any]]:
    rows = conn().execute(
        "SELECT * FROM routing_samples ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            feats = json.loads(r["features"])
        except Exception:
            feats = {}
        out.append({"capability": r["capability"], "features": feats, "chosen": r["chosen"],
                    "success": bool(r["success"]), "duration_ms": r["duration_ms"]})
    return out


# --------------------------------------------------------------------------- #
def snapshot(objective_id: Optional[str] = None) -> Dict[str, Any]:
    """Full authoritative snapshot - used by the readiness report and tests."""
    return {
        "kv": all_kv(),
        "nodes": [n.model_dump() for n in nodes(objective_id)],
        "edges": [e.model_dump() for e in edges()],
        "components": components(),
        "artifacts": artifacts(objective_id),
    }


def reset_all() -> None:
    """Destructive - only used by tests."""
    def _f(c):
        for t in ("kv", "nodes", "edges", "events", "artifacts", "spans",
                  "routing_samples", "components", "approvals", "memory"):
            c.execute(f"DELETE FROM {t}")
    _tx(_f)
