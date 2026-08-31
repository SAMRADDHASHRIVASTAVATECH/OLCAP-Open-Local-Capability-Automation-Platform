"""
MCP SERVER 2 implementations - Research + Knowledge.

olcap-rag      : hybrid BM25 + vector retrieval over a local SQLite index,
                 with citations. Works with zero external services.
olcap-memory   : episodic / semantic / procedural memory with salience decay,
                 shared by every agent, server and workflow.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import state
from ..config import cfg
from ..observability import span
from ..runtime import implements

STOP = set("""a an the and or but if then than that this these those is are was were be been
being of to in on at for with from by as into about over under between through during before
after above below up down out off again further once here there all any both each few more
most other some such no nor not only own same so too very can will just should now""".split())


def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]{2,}", (text or "").lower())
            if t not in STOP]


def _chunks(text: str, size: int = 1200, overlap: int = 180) -> List[str]:
    text = re.sub(r"\n{3,}", "\n\n", text or "")
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += max(1, size - overlap)
    return out or [""]


# --------------------------------------------------------------------------- #
# Vector backend: sqlite-vec when available, deterministic hashing vector
# otherwise. Either way VECTOR_STORAGE keeps working.
# --------------------------------------------------------------------------- #
_DIM = 256


def _hash_vector(text: str, dim: int = _DIM) -> List[float]:
    vec = [0.0] * dim
    toks = _tokens(text)
    if not toks:
        return vec
    for t in toks:
        h = int(hashlib.blake2b(t.encode(), digest_size=8).hexdigest(), 16)
        vec[h % dim] += 1.0
    for i in range(dim):
        if (i % 2) == 0:
            continue
        vec[i] = vec[i] * 0.5 + (vec[i - 1] if i else 0) * 0.5
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (da * db)


class RagIndex:
    """Per-collection SQLite index: documents, chunks, bm25 terms, vectors."""

    def __init__(self, name: str) -> None:
        self.name = name
        root = cfg().indexes
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f"rag_{re.sub(r'[^A-Za-z0-9_]+', '_', name)}.db"
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()
        self._vec_ok = self._try_vec()

    def _init(self) -> None:
        c = self.conn
        c.executescript("""
        CREATE TABLE IF NOT EXISTS docs(
            id TEXT PRIMARY KEY, source TEXT, title TEXT, meta TEXT, created_at REAL);
        CREATE TABLE IF NOT EXISTS chunks(
            id TEXT PRIMARY KEY, doc_id TEXT, seq INTEGER, text TEXT,
            vector TEXT, terms TEXT);
        CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='');
        """)
        c.commit()

    def _try_vec(self) -> bool:
        try:
            import sqlite_vec  # type: ignore
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            v = self.conn.execute("SELECT vec_version()").fetchone()[0]
            self.conn.executescript(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[256]);")
            self.conn.commit()
            self._vec_version = v
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    def add(self, text: str, source: str = "", title: str = "",
            meta: Optional[Dict[str, Any]] = None) -> str:
        doc_id = hashlib.sha1(f"{source}|{title}|{time.time()}".encode()).hexdigest()[:16]
        self.conn.execute("INSERT INTO docs VALUES(?,?,?,?,?)",
                          (doc_id, source, title, json.dumps(meta or {}), time.time()))
        for i, ch in enumerate(_chunks(text)):
            cid = f"{doc_id}:{i}"
            vec = _hash_vector(ch)
            terms = " ".join(_tokens(ch)[:400])
            self.conn.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)",
                              (cid, doc_id, i, ch, json.dumps(vec), terms))
            try:
                self.conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)",
                                  (abs(hash(cid)) % (2 ** 62), ch))
            except Exception:
                pass
            if self._vec_ok:
                try:
                    self.conn.execute(
                        "INSERT INTO chunks_vec(rowid, embedding) VALUES(?,?)",
                        (abs(hash(cid)) % (2 ** 62), json.dumps(vec)))
                except Exception:
                    pass
        self.conn.commit()
        return doc_id

    # ------------------------------------------------------------------ #
    def search(self, query: str, top_k: int = 6, mode: str = "hybrid") -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT c.id, c.doc_id, c.text, c.vector, d.source, d.title "
            "FROM chunks c LEFT JOIN docs d ON d.id=c.doc_id").fetchall()
        if not rows:
            return []
        q = query.lower()
        qtoks = set(_tokens(query))
        scored = []
        qvec = _hash_vector(query)
        for r in rows:
            text = r["text"] or ""
            toks = _tokens(text)
            # BM25-ish lexical score
            tf: Dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            lex = 0.0
            for t in qtoks:
                if t in tf:
                    idf = math.log(1 + len(rows) / (1 + sum(
                        1 for x in rows if t in (x["text"] or "").lower())))
                    lex += (tf[t] * 2.2) / (tf[t] + 1.2) * idf
            lex /= (1 + math.log(1 + max(1, len(toks)) / 300))
            sem = 0.0
            if mode in ("hybrid", "vector"):
                try:
                    sem = _cosine(qvec, json.loads(r["vector"] or "[]"))
                except Exception:
                    sem = 0.0
            phrase = 1.0 if len(q) > 12 and q[:40] in text.lower() else 0.0
            score = (0.0 if mode == "vector" else lex) + \
                    (0.0 if mode == "lexical" else sem * 3.0) + phrase * 0.5
            scored.append({"chunk_id": r["id"], "doc_id": r["doc_id"],
                           "text": text, "source": r["source"], "title": r["title"],
                           "score": round(score, 4), "lexical": round(lex, 4),
                           "semantic": round(sem, 4)})
        scored.sort(key=lambda x: -x["score"])
        return scored[:top_k]

    def stats(self) -> Dict[str, Any]:
        d = self.conn.execute("SELECT COUNT(*) n FROM docs").fetchone()["n"]
        c = self.conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
        return {"collection": self.name, "docs": d, "chunks": c,
                "vector_backend": "sqlite-vec" if self._vec_ok else "hashed-deterministic",
                "path": str(self.path)}


_INDEXES: Dict[str, RagIndex] = {}


def _index(name: str) -> RagIndex:
    if name not in _INDEXES:
        _INDEXES[name] = RagIndex(name)
    return _INDEXES[name]


# --------------------------------------------------------------------------- #
# RAG
# --------------------------------------------------------------------------- #
def _read_source(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if p.suffix.lower() in (".pdf", ".docx", ".doc"):
        from ..runtime import execute
        out = execute("DOCUMENT_INTELLIGENCE",
                      {"action": "extract", "path": str(p)},
                      method="document_process")
        return out["result"].get("text", "")
    return p.read_text(encoding="utf-8", errors="replace")


@implements("RAG", "olcap-rag")
def rag_query(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "query").lower()
    collection = params.get("collection") or "default"
    idx = _index(collection)
    with span("rag.op", "capability", {"action": action, "collection": collection}) as sp:
        if action in ("ingest", "add", "index"):
            n = 0
            for p in (params.get("paths") or []):
                idx.add(_read_source(p), source=p, title=Path(p).name)
                n += 1
            for u in (params.get("urls") or []):
                from ..runtime import execute
                out = execute("WEB_EXTRACT", {"url": u}, method="web_extract")
                idx.add(out["result"].get("text", ""), source=u, title=u)
                n += 1
            if params.get("text"):
                idx.add(params["text"], source=params.get("source", "inline"),
                        title=params.get("title", "inline"))
                n += 1
            sp.set(ingested=n)
            return {"ok": True, "action": "ingest", "ingested": n,
                    "stats": idx.stats()}
        if action == "stats":
            return {"ok": True, "stats": idx.stats()}
        if action == "list":
            return {"ok": True, "collections": sorted(
                p.stem.replace("rag_", "") for p in cfg().indexes.glob("rag_*.db"))}

        query = params.get("query") or ""
        top_k = int(params.get("top_k", 6) or 6)
        mode = (params.get("mode") or "hybrid").lower()
        hits = idx.search(query, top_k=top_k, mode=mode)
        context = "\n\n".join(f"[{i+1}] ({h['source'] or h['doc_id']}) {h['text'][:1200]}"
                              for i, h in enumerate(hits))
        from ..llm import llm
        res = llm().complete(
            system=("Answer strictly from the numbered context. Cite sources as [n]. "
                    "If the context is insufficient, say so explicitly."),
            prompt=f"Question: {query}\n\nContext:\n{context}\n\nAnswer:")
        sp.set(hits=len(hits))
        return {"ok": True, "action": "query", "answer": res.get("text", ""),
                "citations": [{"n": i + 1, "source": h["source"],
                               "title": h["title"], "score": h["score"]}
                              for i, h in enumerate(hits)],
                "chunks": hits, "collection": collection, "mode": mode,
                "model_backend": res.get("backend"),
                "grounded": bool(hits)}


@implements("KNOWLEDGE_SEARCH", "olcap-rag")
def knowledge_search(params: Dict[str, Any]) -> Dict[str, Any]:
    query = params.get("query") or ""
    collection = params.get("collection") or "default"
    top_k = int(params.get("top_k", 8) or 8)
    mode = (params.get("mode") or "hybrid").lower()
    hits = _index(collection).search(query, top_k=top_k, mode=mode)
    return {"ok": True, "hits": hits, "collection": collection, "query": query}


# --------------------------------------------------------------------------- #
# MEMORY
# --------------------------------------------------------------------------- #
def _score_text(text: str, query: str) -> float:
    qt = set(_tokens(query))
    tt = _tokens(text)
    if not qt or not tt:
        return 0.0
    overlap = len(qt & set(tt)) / len(qt)
    tf = sum(1 for t in tt if t in qt) / len(tt)
    return round(overlap * 0.7 + tf * 0.3, 4)


@implements("MEMORY", "olcap-memory")
def memory_op(params: Dict[str, Any]) -> Dict[str, Any]:
    action = (params.get("action") or "search").lower()
    kind = params.get("kind") or "semantic"
    limit = int(params.get("limit", 10) or 10)
    c = state.conn()

    if action in ("put", "add", "write", "store"):
        text = params.get("text") or ""
        if not text:
            raise ValueError("text is required for memory put")
        from ..models import new_id
        mid = new_id("mem")
        sal = float(params.get("salience", 0.5))
        c.execute("""INSERT INTO memory(id,kind,text,meta,salience,created_at,
                     last_access,access_count) VALUES(?,?,?,?,?,?,?,?)""",
                  (mid, kind, text, json.dumps(params.get("meta") or {}), sal,
                   time.time(), time.time(), 0))
        c.commit()
        state.emit("memory.put", "memory", {"id": mid, "kind": kind})
        return {"ok": True, "id": mid, "action": "put"}

    if action in ("get", "fetch"):
        row = c.execute("SELECT * FROM memory WHERE id=?", (params.get("id"),)).fetchone()
        return {"ok": row is not None,
                "item": dict(row) if row else None}

    if action in ("search", "query", "recall"):
        query = params.get("query") or params.get("text") or ""
        rows = c.execute("SELECT * FROM memory").fetchall()
        now = time.time()
        scored = []
        for r in rows:
            age_days = (now - (r["created_at"] or now)) / 86400.0
            decay = math.exp(-age_days / 30.0)          # 30-day half-life-ish
            s = _score_text(r["text"] or "", query) * (0.6 + 0.4 * decay) \
                + float(r["salience"] or 0.5) * 0.15
            if s > 0.01:
                scored.append({**dict(r), "score": round(s, 4),
                               "decay": round(decay, 3)})
                c.execute("UPDATE memory SET access_count=access_count+1, "
                          "last_access=? WHERE id=?", (now, r["id"]))
        c.commit()
        scored.sort(key=lambda x: -x["score"])
        for x in scored[:limit]:
            try:
                x["meta"] = json.loads(x["meta"] or "{}")
            except Exception:
                pass
        return {"ok": True, "items": scored[:limit], "action": "search",
                "kind": kind, "total": len(rows)}

    if action == "list":
        rows = c.execute("SELECT * FROM memory ORDER BY created_at DESC LIMIT ?",
                         (limit,)).fetchall()
        return {"ok": True, "items": [dict(r) for r in rows], "action": "list"}

    if action in ("forget", "delete"):
        ident = params.get("id")
        if ident:
            c.execute("DELETE FROM memory WHERE id=?", (ident,))
        else:
            c.execute("DELETE FROM memory WHERE kind=?", (kind,))
        c.commit()
        return {"ok": True, "action": "forget", "id": ident}

    if action == "stats":
        rows = c.execute("SELECT kind, COUNT(*) n FROM memory GROUP BY kind").fetchall()
        return {"ok": True, "by_kind": {r["kind"]: r["n"] for r in rows},
                "total": sum(r["n"] for r in rows)}

    raise ValueError(
        f"unknown memory action: {action!r} "
        f"(valid: put|add|write|store, get|fetch, search|query|recall, "
        f"list, stats)")


# --------------------------------------------------------------------------- #
# DOCUMENT_INTELLIGENCE - plain text / markdown / html / structured data
# --------------------------------------------------------------------------- #
_TEXT_EXT = {".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl",
             ".csv", ".tsv", ".yaml", ".yml", ".html", ".htm", ".xml", ".ini",
             ".cfg", ".toml", ".py", ".js", ".ts", ".sql", ".sh", ".ps1", ".tex"}


@implements("DOCUMENT_INTELLIGENCE", "olcap-text-extract")
def document_process_text(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Direct reader for text-like documents. Cheap, dependency-free and always
    available - the baseline every heavier parser can be compared against.
    """
    path = params.get("path") or ""
    url = params.get("url") or ""
    action = (params.get("action") or "extract").lower()

    if url and not path:
        from ..runtime import execute
        out = execute("WEB_EXTRACT", {"url": url}, method="web_extract")
        return {"ok": True, "text": (out.get("result") or {}).get("text", ""),
                "metadata": {"source": url}, "sections": [], "tables": []}

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if p.suffix.lower() not in _TEXT_EXT:
        return {"ok": False,
                "error": f"olcap-text-extract does not handle '{p.suffix}'; "
                         f"use a binary-capable parser"}

    raw = p.read_text(encoding="utf-8", errors="replace")
    text = raw
    if p.suffix.lower() in (".html", ".htm", ".xml"):
        try:
            import trafilatura
            text = trafilatura.extract(raw) or re.sub(r"<[^>]+>", " ", raw)
        except Exception:
            text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()

    sections = []
    if p.suffix.lower() in (".md", ".markdown", ".rst"):
        cur_title, buf = "", []
        for line in raw.splitlines():
            if line.startswith("#"):
                if cur_title and buf:
                    sections.append({"title": cur_title,
                                     "text": "\n".join(buf).strip()})
                cur_title = line.lstrip("#").strip()
                buf = []
            else:
                buf.append(line)
        if cur_title and buf:
            sections.append({"title": cur_title, "text": "\n".join(buf).strip()})

    tables: List[List[Any]] = []
    if p.suffix.lower() in (".csv", ".tsv"):
        import csv as _csv
        delim = "\t" if p.suffix.lower() == ".tsv" else ","
        with p.open(newline="", encoding="utf-8", errors="replace") as fh:
            tables = [row for row in _csv.reader(fh, delimiter=delim)][:200]

    return {"ok": True, "text": text, "sections": sections, "tables": tables,
            "metadata": {"source": str(p), "chars": len(text),
                         "engine": "olcap-text-extract"}}
