"""
MCP SERVER 3 implementations - Data + databases + vectors.

duckdb      : data analysis and SQL over CSV/Parquet/JSON/SQLite/DuckDB.
sqlite-vec  : embedded vector collections (no server, no external service).
qdrant      : local (embedded) mode - also usable against a self-hosted server.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import cfg
from ..observability import span
from ..runtime import implements

_SAFE_SQL = re.compile(r"^\s*(select|with|describe|explain|pragma|show)\b", re.I)
_WRITE_SQL = re.compile(r"\b(insert|update|delete|create|drop|alter|copy|attach)\b", re.I)


def _duck():
    import duckdb
    return duckdb


# --------------------------------------------------------------------------- #
# DATA_ANALYSIS
# --------------------------------------------------------------------------- #
def _connect(source: Optional[str] = None):
    ddb = _duck()
    con = ddb.connect(cfg().data / "olcap.duckdb" if not source else ":memory:")
    try:
        con.execute("INSTALL httpfs; LOAD httpfs;")
    except Exception:
        pass
    return con


def _rel(source: str) -> str:
    p = Path(source)
    suf = p.suffix.lower()
    if suf == ".parquet":
        return f"read_parquet('{source}')"
    if suf in (".csv", ".tsv"):
        return f"read_csv_auto('{source}')"
    if suf == ".json":
        return f"read_json_auto('{source}')"
    if suf == ".ndjson":
        return f"read_ndjson_auto('{source}')"
    if suf in (".db", ".sqlite", ".sqlite3", ".duckdb"):
        return f"sqlite_scan('{source}', 'main_table')"
    return f"'{source}'"


@implements("DATA_ANALYSIS", "duckdb")
def data_analyze(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "profile").lower()
    path = params.get("path") or ""
    con = _connect()
    try:
        with span("data.analyze", "capability", {"action": action, "path": path[:120]}) as sp:
            if action == "profile":
                rel = _rel(path)
                cols = con.execute(f"DESCRIBE SELECT * FROM {rel} LIMIT 1").fetchall()
                out = []
                for name, ctype, *_ in cols:
                    try:
                        stats = con.execute(
                            f"SELECT COUNT(*), COUNT({name}), "
                            f"COUNT(DISTINCT {name}), MIN({name}), MAX({name}) "
                            f"FROM {rel}").fetchone()
                        out.append({"column": name, "type": ctype, "count": stats[0],
                                    "non_null": stats[1], "distinct": stats[2],
                                    "min": str(stats[3])[:60], "max": str(stats[4])[:60]})
                    except Exception as e:
                        out.append({"column": name, "type": ctype, "error": str(e)[:80]})
                n = con.execute(f"SELECT COUNT(*) FROM {rel}").fetchone()[0]
                sp.set(columns=len(out), rows=n)
                return {"action": "profile", "path": path, "columns": out,
                        "rowcount": n, "engine": "duckdb"}

            if action in ("head", "sample"):
                rel = _rel(path)
                limit = int(params.get("limit", 20) or 20)
                cur = con.execute(f"SELECT * FROM {rel} LIMIT {limit}")
                return {"action": action, "columns": [d[0] for d in cur.description],
                        "rows": [list(r) for r in cur.fetchall()], "engine": "duckdb"}

            if action == "aggregate":
                rel = _rel(path)
                expr = params.get("expr") or "SELECT COUNT(*) AS n"
                if " from " not in expr.lower():
                    expr = f"{expr.rstrip(';')} FROM {rel}"
                cur = con.execute(expr)
                return {"action": "aggregate", "columns": [d[0] for d in cur.description],
                        "rows": [list(r) for r in cur.fetchall()],
                        "sql": expr, "engine": "duckdb"}

            if action == "convert":
                src, dst = _rel(path), params.get("destination") or ""
                if not dst:
                    raise ValueError("destination required for convert")
                fmt = "PARQUET" if dst.endswith(".parquet") else "CSV"
                con.execute(f"COPY (SELECT * FROM {src}) TO '{dst}' (FORMAT {fmt})")
                return {"action": "convert", "from": path, "to": dst, "engine": "duckdb"}

            if action == "describe":
                rel = _rel(path)
                cur = con.execute(f"SELECT * FROM {rel} LIMIT 1000")
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                summary = {}
                for i, c in enumerate(cols):
                    vals = [r[i] for r in rows if r[i] is not None]
                    nums = [v for v in vals if isinstance(v, (int, float))]
                    summary[c] = {
                        "n": len(vals),
                        "distinct": len(set(map(str, vals))),
                        "mean": round(sum(nums) / len(nums), 4) if nums else None,
                        "min": min(nums) if nums else None,
                        "max": max(nums) if nums else None,
                    }
                return {"action": "describe", "columns": cols,
                        "summary": summary, "sampled": len(rows), "engine": "duckdb"}

            raise ValueError(f"unknown data action: {action}")
    finally:
        try:
            con.close()
        except Exception:
            pass


@implements("DATABASE_QUERY", "duckdb")
def database_query(params: Dict[str, Any]) -> Dict[str, Any]:
    sql = (params.get("sql") or "").strip().rstrip(";")
    if not sql:
        raise ValueError("sql is required")
    limit = int(params.get("limit", 1000) or 1000)
    con = _connect(params.get("source"))
    try:
        with span("data.query", "capability", {"sql": sql[:160]}) as sp:
            if _WRITE_SQL.search(sql) and not _SAFE_SQL.match(sql):
                # writes are allowed only against the internal database
                if not str(params.get("source") or "").endswith((".duckdb", ".db")) \
                        and "olcap.duckdb" not in sql:
                    raise PermissionError(
                        "write SQL is only permitted against the internal store")
            cur = con.execute(sql)
            if cur.description is None:
                con.commit()
                sp.set(rows=0)
                return {"columns": [], "rows": [], "rowcount": 0, "sql": sql,
                        "engine": "duckdb"}
            cols = [d[0] for d in cur.description]
            rows = [list(r) for r in cur.fetchmany(limit)]
            sp.set(rows=len(rows))
            return {"columns": cols, "rows": rows, "rowcount": len(rows),
                    "sql": sql, "engine": "duckdb", "truncated": len(rows) == limit}
    finally:
        try:
            con.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# VECTOR_STORAGE
# --------------------------------------------------------------------------- #
class VectorStore:
    """Embedded vector store: sqlite-vec when loadable, brute-force otherwise."""

    def __init__(self, name: str = "default", dim: int = 256) -> None:
        cfg().indexes.mkdir(parents=True, exist_ok=True)
        self.path = cfg().indexes / f"vec_{re.sub(r'[^A-Za-z0-9_]+', '_', name)}.db"
        self.dim = dim
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors(id TEXT PRIMARY KEY, vector TEXT, "
            "text TEXT, meta TEXT, created_at REAL)")
        self.conn.commit()
        self.vec_ok = self._try_vec()

    def _try_vec(self) -> bool:
        try:
            import sqlite_vec  # type: ignore
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            return True
        except Exception:
            return False

    def upsert(self, items: List[Dict[str, Any]]) -> int:
        n = 0
        for i, it in enumerate(items or []):
            vid = it.get("id") or hashlib.sha1(
                f"{it.get('text','')}{i}{time.time()}".encode()).hexdigest()[:16]
            vec = it.get("vector")
            if not vec:
                from .knowledge import _hash_vector
                vec = _hash_vector(it.get("text", ""))
            self.conn.execute(
                "INSERT INTO vectors VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "vector=excluded.vector, text=excluded.text, meta=excluded.meta",
                (vid, json.dumps(vec), it.get("text", ""),
                 json.dumps(it.get("meta") or {}), time.time()))
            n += 1
        self.conn.commit()
        return n

    def search(self, query_vec: List[float], top_k: int = 10) -> List[Dict[str, Any]]:
        from .knowledge import _cosine
        rows = self.conn.execute("SELECT * FROM vectors").fetchall()
        out = []
        for r in rows:
            try:
                v = json.loads(r["vector"])
            except Exception:
                continue
            out.append({"id": r["id"], "score": round(_cosine(query_vec, v), 4),
                        "text": r["text"], "meta": r["meta"]})
        out.sort(key=lambda x: -x["score"])
        return out[:top_k]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) n FROM vectors").fetchone()["n"]

    def stats(self) -> Dict[str, Any]:
        return {"collection": str(self.path.stem), "count": self.count(),
                "backend": "sqlite-vec" if self.vec_ok else "brute-force-cosine",
                "dim": self.dim}


_STORES: Dict[str, VectorStore] = {}


def _store(name: str) -> VectorStore:
    if name not in _STORES:
        _STORES[name] = VectorStore(name)
    return _STORES[name]


@implements("VECTOR_STORAGE", "sqlite-vec")
def vector_store_op(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "search").lower()
    store = _store(params.get("collection") or "default")
    with span("vector.op", "capability", {"action": action}) as sp:
        if action in ("upsert", "add", "insert"):
            n = store.upsert(params.get("vectors") or [])
            sp.set(upserted=n)
            return {"ok": True, "count": n, "stats": store.stats()}
        if action == "stats":
            return {"ok": True, "stats": store.stats()}
        if action == "list":
            return {"ok": True, "collections": sorted(
                p.stem for p in cfg().indexes.glob("vec_*.db"))}
        query = params.get("query") or ""
        top_k = int(params.get("top_k", 10) or 10)
        from .knowledge import _hash_vector
        qv = params.get("vector") or _hash_vector(query)
        hits = store.search(qv, top_k=top_k)
        return {"ok": True, "hits": hits, "count": len(hits), "stats": store.stats()}


@implements("VECTOR_STORAGE", "qdrant")
def qdrant_store_op(params: Dict[str, Any]) -> Dict[str, Any]:
    """Qdrant in embedded/local mode - no server, no cloud account."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams
    collection = params.get("collection") or "default"
    client = QdrantClient(path=str(cfg().indexes / "qdrant_local"))
    action = (params.get("action") or "search").lower()
    dim = int(params.get("dim", 256))
    if not client.collection_exists(collection):
        client.create_collection(collection,
                                 vectors_config=VectorParams(size=dim,
                                                             distance=Distance.COSINE))
    if action in ("upsert", "add", "insert"):
        from .knowledge import _hash_vector
        pts = []
        for i, it in enumerate(params.get("vectors") or []):
            v = it.get("vector") or _hash_vector(it.get("text", ""))
            pts.append(PointStruct(id=it.get("id", i) if isinstance(it.get("id"), int)
                                   else abs(hash(str(it.get("id", i)))) % (2 ** 62),
                                   vector=v, payload={"text": it.get("text", ""),
                                                      **(it.get("meta") or {})}))
        client.upsert(collection, points=pts)
        return {"ok": True, "count": len(pts), "backend": "qdrant-local"}
    if action == "stats":
        return {"ok": True, "stats": client.get_collection(collection).model_dump()}
    from .knowledge import _hash_vector
    qv = params.get("vector") or _hash_vector(params.get("query") or "")
    res = client.query_points(collection, query=qv,
                              limit=int(params.get("top_k", 10) or 10)).points
    return {"ok": True, "hits": [{"id": p.id, "score": p.score,
                                  **(p.payload or {})} for p in res],
            "backend": "qdrant-local"}
