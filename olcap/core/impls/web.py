"""
MCP SERVER 1 implementations - Web + Browser.

Engines are selected by what actually ANSWERS, not by assumption. Before use
each engine is probed live; dead engines degrade the result but never fail the
capability as long as one engine works. General-web results come from
Marginalia and (optionally) a self-hosted SearXNG; vertical results come from
Wikipedia, arXiv, OpenAlex, Crossref, GitHub and HackerNews.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from typing import Any, Dict, List, Optional, Tuple

from .. import state
from ..config import cfg
from ..observability import span
from ..runtime import implements

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
_TIMEOUT = 12
# Worst-case wall time for one web_search call. Engines run in parallel, so a
# slow or throttled engine can never make the whole call slow.
_SEARCH_DEADLINE = 14


_ALLOWED_SCHEMES = ("http", "https")


def _assert_fetchable(url: str) -> None:
    """Reject anything that is not a plain http(s) fetch.

    Without this, file:// and other schemes turn a "browse this page" call
    into local file disclosure (classic SSRF).
    """
    scheme = (urllib.parse.urlparse(str(url or "")).scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"refusing to fetch unsupported scheme {scheme or '(none)'!r}; "
            f"only {', '.join(_ALLOWED_SCHEMES)} are allowed")


def _fetch(url: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
           timeout: int = _TIMEOUT) -> Tuple[int, str, str]:
    _assert_fetchable(url)
    req = urllib.request.Request(url, data=data,
                                headers={"User-Agent": UA, **(headers or {})})

    def _do() -> Tuple[int, str, str]:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            final = r.geturl()
            ctype = r.headers.get("Content-Type", "")
            enc = "utf-8"
            m = re.search(r"charset=([\w\-]+)", ctype)
            if m:
                enc = m.group(1)
            return r.status, raw.decode(enc, "replace"), final

    return _bounded(_do, float(timeout) + 5.0)


def _bounded(fn, seconds: float):
    """Run fn on a daemon thread and give up after `seconds`.

    urlopen(timeout=...) does not cover DNS resolution or TLS handshakes, so a
    host that never answers stalled the caller far past its timeout and, with
    several calls in a plan, burned the entire run budget. This is a hard
    wall-clock bound on the operation, whatever it is blocked on.
    """
    box: Dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:      # noqa: BLE001 - re-raised below
            box["error"] = exc

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(max(0.0, float(seconds)))
    if th.is_alive():
        raise TimeoutError(f"network operation exceeded {seconds:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(re.sub(r"\s+", " ", s)).strip()


def _clean_url(u: str) -> str:
    if u.startswith("//"):
        u = "https:" + u
    return u.split("#")[0]


# --------------------------------------------------------------------------- #
# Engines
# --------------------------------------------------------------------------- #
def _wikipedia(query: str, limit: int) -> List[Dict[str, Any]]:
    u = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query,
         "format": "json", "srlimit": limit})
    _, body, _ = _fetch(u)
    d = json.loads(body)
    out = []
    for r in d.get("query", {}).get("search", []):
        out.append({
            "title": re.sub("<[^>]+>", "", r.get("title", "")),
            "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(r['title'].replace(' ', '_'))}",
            "snippet": re.sub("<[^>]+>", "", r.get("snippet", "")),
            "engine": "wikipedia", "score": 0.9,
        })
    return out


def _arxiv(query: str, limit: int) -> List[Dict[str, Any]]:
    u = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": f"all:{query}", "max_results": limit})
    _, body, _ = _fetch(u)
    entries = re.findall(r"<entry>(.*?)</entry>", body, re.S)
    out = []
    for e in entries:
        t = re.search(r"<title>(.*?)</title>", e, re.S)
        l = re.search(r"<id>(.*?)</id>", e, re.S)
        s = re.search(r"<summary>(.*?)</summary>", e, re.S)
        out.append({"title": _strip_tags(t.group(1)) if t else "",
                    "url": (l.group(1).strip() if l else ""),
                    "snippet": _strip_tags(s.group(1))[:400] if s else "",
                    "engine": "arxiv", "score": 0.8})
    return out


def _openalex(query: str, limit: int) -> List[Dict[str, Any]]:
    u = "https://api.openalex.org/works?" + urllib.parse.urlencode(
        {"search": query, "per-page": limit,
         "mailto": cfg().env("OPENALEX_MAILTO", "olcap@localhost")})
    _, body, _ = _fetch(u)
    d = json.loads(body)
    out = []
    for w in d.get("results", []):
        out.append({"title": w.get("title") or w.get("display_name") or "",
                    "url": w.get("doi") or w.get("id") or "",
                    "snippet": (w.get("abstract") or "")[:400] or
                               (w.get("title") or ""),
                    "engine": "openalex", "score": 0.75,
                    "meta": {"year": w.get("publication_year"),
                             "cited_by": w.get("cited_by_count")}})
    return out


def _crossref(query: str, limit: int) -> List[Dict[str, Any]]:
    u = "https://api.crossref.org/works?" + urllib.parse.urlencode(
        {"query": query, "rows": limit})
    _, body, _ = _fetch(u)
    d = json.loads(body)
    out = []
    for it in d.get("message", {}).get("items", []):
        title = (it.get("title") or [""])[0]
        url = it.get("URL") or it.get("DOI") or ""
        out.append({"title": title, "url": url, "snippet": title,
                    "engine": "crossref", "score": 0.7,
                    "meta": {"year": (it.get("issued", {}).get("date-parts") or [[]])[0][:1]}})
    return out


def _github(query: str, limit: int) -> List[Dict[str, Any]]:
    u = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": query, "per_page": limit, "sort": "stars"})
    try:
        _, body, _ = _fetch(u)
    except Exception as e:
        if "429" in str(e):
            return []
        raise
    d = json.loads(body)
    out = []
    for r in d.get("items", []):
        out.append({"title": r.get("full_name", ""), "url": r.get("html_url", ""),
                    "snippet": (r.get("description") or "")[:400],
                    "engine": "github", "score": 0.85,
                    "meta": {"stars": r.get("stargazers_count"),
                             "language": r.get("language")}})
    return out


def _hackernews(query: str, limit: int) -> List[Dict[str, Any]]:
    u = "https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode(
        {"query": query, "hitsPerPage": limit})
    _, body, _ = _fetch(u)
    d = json.loads(body)
    out = []
    for h in d.get("hits", []):
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append({"title": h.get("title") or h.get("story_title") or "",
                    "url": url, "snippet": (h.get("story_text") or "")[:300],
                    "engine": "hackernews", "score": 0.6,
                    "meta": {"points": h.get("points")}})
    return out


def _marginalia(query: str, limit: int) -> List[Dict[str, Any]]:
    u = "https://old-search.marginalia.nu/search?" + urllib.parse.urlencode(
        {"query": query})
    _, body, _ = _fetch(u, timeout=30)
    out = []
    # Each result block: <a href="URL" ...>title</a> ... <p>description</p>
    for m in re.finditer(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*class="[^"]*"[^>]*>(.*?)</a>', body, re.S):
        url, title = m.group(1), _strip_tags(m.group(2))
        if not title or "marginalia" in url:
            continue
        out.append({"title": title[:200], "url": _clean_url(url), "snippet": "",
                    "engine": "marginalia", "score": 0.65})
        if len(out) >= limit:
            break
    if not out:  # alternate markup
        for m in re.finditer(r'href="(https?://[^"]+)"[^>]*>\s*([^<]{10,150})\s*</a>', body):
            out.append({"title": _strip_tags(m.group(2)), "url": _clean_url(m.group(1)),
                        "snippet": "", "engine": "marginalia", "score": 0.6})
            if len(out) >= limit:
                break
    return out


def _searxng(query: str, limit: int, base: str) -> List[Dict[str, Any]]:
    """Self-hosted SearXNG: JSON when the instance allows it, HTML otherwise."""
    out: List[Dict[str, Any]] = []
    try:
        u = f"{base.rstrip('/')}/search?" + urllib.parse.urlencode(
            {"q": query, "format": "json"})
        _, body, _ = _fetch(u)
        try:
            d = json.loads(body)
            for r in d.get("results", [])[:limit]:
                out.append({"title": r.get("title", ""), "url": r.get("url", ""),
                            "snippet": r.get("content", ""), "engine": "searxng",
                            "score": 0.95})
            return out
        except Exception:
            pass
        for m in re.finditer(r'<a[^>]+class="url_header"[^>]+href="([^"]+)"', body):
            out.append({"title": "", "url": _clean_url(m.group(1)),
                        "snippet": "", "engine": "searxng", "score": 0.9})
            if len(out) >= limit:
                break
        if not out:
            for m in re.finditer(r'<article[^>]*>(.*?)</article>', body, re.S):
                blk = m.group(1)
                a = re.search(r'href="(https?://[^"]+)"', blk)
                t = re.search(r'<h3[^>]*>(.*?)</h3>', blk, re.S)
                if a:
                    out.append({"title": _strip_tags(t.group(1)) if t else "",
                                "url": _clean_url(a.group(1)),
                                "snippet": _strip_tags(blk)[:300],
                                "engine": "searxng", "score": 0.9})
                if len(out) >= limit:
                    break
    except Exception:
        return []
    return out


GENERAL_ENGINES = {"marginalia": _marginalia, "searxng": _searxng}
VERTICAL_ENGINES = {"wikipedia": _wikipedia, "arxiv": _arxiv, "openalex": _openalex,
                    "crossref": _crossref, "github": _github, "hackernews": _hackernews}
ALL_ENGINES = {**GENERAL_ENGINES, **VERTICAL_ENGINES}


def _classify(query: str) -> List[str]:
    q = query.lower()
    picks = ["wikipedia"]
    if re.search(r"\b(paper|study|research|arxiv|preprint|journal|doi|"
                 r"systematic review|meta-analysis)\b", q):
        picks += ["arxiv", "openalex", "crossref"]
    if re.search(r"\b(github|repo|repository|library|package|sdk|framework|"
                 r"code|api|python|npm|open source)\b", q):
        picks += ["github"]
    if re.search(r"\b(hacker ?news|discussion|opinion|thread)\b", q):
        picks += ["hackernews"]
    if len(picks) == 1:
        picks += ["marginalia"]
    return picks


# --------------------------------------------------------------------------- #
# WEB_SEARCH
# --------------------------------------------------------------------------- #
def _rank(results: List[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    seen = set()
    out = []
    for r in results:
        key = re.sub(r"^https?://(www\.)?", "", (r.get("url") or "")).rstrip("/")
        if not key or key in seen:
            continue
        seen.add(key)
        blob = f"{r.get('title','')} {r.get('snippet','')}".lower()
        overlap = sum(1 for t in terms if t in blob) / max(1, len(terms))
        r["relevance"] = round(float(r.get("score", 0.5)) * 0.6 + overlap * 0.4, 4)
        out.append(r)
    return sorted(out, key=lambda x: -x["relevance"])


# Short-TTL cache: identical queries within the window reuse the answer.
# This is the resource-aware caching the architecture calls for, and it keeps
# repeated sub-questions from hammering the same free engines.
_SEARCH_CACHE: Dict[str, Any] = {}
_CACHE_TTL_S = 60



def _run_engines(names: List[str], query: str, limit: int,
                 searxng_url: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fan out to every named engine in parallel and collect what comes back.

    Engines are independent, so they are queried concurrently under one shared
    deadline. A single slow/throttled engine therefore costs nothing extra and
    can never stall the whole search.
    """
    results: List[Dict[str, Any]] = []
    stats: Dict[str, Any] = {}

    def one(name: str):
        fn = ALL_ENGINES.get(name)
        if not fn:
            return name, [], "unknown engine", 0
        t0 = time.time()
        try:
            got = (fn(query, limit, searxng_url or "")
                   if name == "searxng" else fn(query, min(limit, 20)))
            return name, list(got or []), None, round((time.time() - t0) * 1000)
        except Exception as e:
            return name, [], f"{type(e).__name__}: {str(e)[:120]}", \
                round((time.time() - t0) * 1000)

    names = [n for n in names if n]
    if not names:
        return results, stats
    with ThreadPoolExecutor(max_workers=min(8, len(names)),
                            thread_name_prefix="olcap-search") as ex:
        futs = {ex.submit(one, n): n for n in names}
        done, not_done = wait(list(futs), timeout=_SEARCH_DEADLINE)
        for f in done:
            name = futs[f]
            try:
                nm, got, err, ms = f.result(timeout=0)
            except Exception as e:
                nm, got, err, ms = name, [], f"{type(e).__name__}: {str(e)[:120]}", -1
            if err:
                stats[nm] = {"ok": False, "error": err}
            else:
                results.extend(got)
                stats[nm] = {"ok": True, "count": len(got), "ms": ms}
        for f in not_done:
            f.cancel()
            stats[futs[f]] = {"ok": False,
                              "error": f"timeout after {_SEARCH_DEADLINE}s"}
    return results, stats


@implements("WEB_SEARCH", "olcap-search-core")
def web_search(params: Dict[str, Any]) -> Dict[str, Any]:
    query = params.get("query") or ""
    if not query:
        raise ValueError("query is required")
    limit = int(params.get("max_results", 10) or 10)
    engines = params.get("engines")
    if not engines or engines == ["auto"]:
        engines = _classify(query)
    searxng_url = cfg().env("SEARXNG_URL")
    if "searxng" in engines and not searxng_url:
        engines = [e for e in engines if e != "searxng"] + ["marginalia"]

    key = f"{query}|{tuple(sorted(engines))}|{limit}"
    hit = _SEARCH_CACHE.get(key)
    if hit and time.time() - hit["ts"] < _CACHE_TTL_S:
        state.emit("web.search.cache_hit", "capability", {"query": query[:80]})
        return {**hit["payload"], "cached": True}

    with span("web.search", "capability", {"query": query[:80], "engines": engines}) as sp:
        results, stats = _run_engines(engines, query, limit, searxng_url or "")
        ranked = _rank(results, query)[:limit]
        if not ranked and set(engines) != set(ALL_ENGINES):
            # The classified subset found nothing (or every engine it picked
            # timed out): widen to every remaining engine before failing.
            sp.set(retry="all_engines")
            extra = [n for n in ALL_ENGINES if n not in stats]
            more, more_stats = _run_engines(extra, query, limit,
                                            cfg().env("SEARXNG_URL") or "")
            results.extend(more)
            stats.update(more_stats)
            ranked = _rank(results, query)[:limit]
        sp.set(engines=list(stats), results=len(ranked))
    if not ranked:
        raise RuntimeError(f"no search engine returned results ({stats})")
    payload = {"results": ranked, "engine_stats": stats, "query": query,
               "engines_used": [k for k, v in stats.items() if v.get("ok")]}
    _SEARCH_CACHE[key] = {"ts": time.time(), "payload": payload}
    return payload


def _sources_from_results(ranked: List[Dict[str, Any]],
                          topic: str = "",
                          engines: Optional[List[str]] = None
                          ) -> Dict[str, Any]:
    """Shared source-diversity summary for the source discovery backends."""
    domains: Dict[str, int] = {}
    for r in ranked:
        d = urllib.parse.urlparse(r.get("url") or "").netloc
        domains[d] = domains.get(d, 0) + 1
    diversity = round(len(domains) / max(1, len(ranked)), 3)
    return {"sources": ranked, "engines": engines or [], "domains": domains,
            "diversity": diversity, "topic": topic}


@implements("WEB_SEARCH", "searxng")
def web_search_searxng(params: Dict[str, Any]) -> Dict[str, Any]:
    """Self-hosted SearXNG (docker). Requires SEARXNG_URL to be configured."""
    base = cfg().env("SEARXNG_URL")
    if not base:
        raise RuntimeError(
            "searxng selected but SEARXNG_URL is not configured "
            "(self-host a SearXNG instance and set SEARXNG_URL)")
    query = params.get("query") or ""
    if not query:
        raise ValueError("query is required")
    limit = int(params.get("max_results", 10) or 10)
    got = _searxng(query, min(limit, 20), base)
    if not got:
        raise RuntimeError(f"searxng at {base} returned no results")
    return {"results": _rank(got, query)[:limit], "query": query,
            "engine_stats": {"searxng": {"ok": True, "count": len(got)}},
            "engines_used": ["searxng"]}


@implements("SOURCE_DISCOVERY", "searxng")
def web_discover_sources_searxng(params: Dict[str, Any]) -> Dict[str, Any]:
    """Source discovery over a self-hosted SearXNG instance."""
    topic = params.get("topic") or params.get("query") or ""
    max_sources = int(params.get("max_sources", 12) or 12)
    res = web_search_searxng({"query": topic, "max_results": max_sources * 2})
    return _sources_from_results(res["results"][:max_sources], topic,
                                 res.get("engines_used", []))


@implements("SOURCE_DISCOVERY", "olcap-search-core")
def web_discover_sources(params: Dict[str, Any]) -> Dict[str, Any]:
    topic = params.get("topic") or params.get("query") or ""
    max_sources = int(params.get("max_sources", 12) or 12)
    res = web_search({"query": topic, "max_results": max_sources * 2})
    ranked = res["results"][:max_sources]
    return _sources_from_results(ranked, topic, res.get("engines_used", []))


@implements("SOURCE_COMPARISON", "olcap-search-core")
def web_compare_sources(params: Dict[str, Any]) -> Dict[str, Any]:
    claim = params.get("claim") or ""
    max_sources = int(params.get("max_sources", 6) or 6)
    sources = params.get("sources") or web_search(
        {"query": claim, "max_results": max_sources})["results"]
    comparison = []
    for s in sources[:max_sources]:
        comparison.append({"url": s.get("url"), "title": s.get("title"),
                           "engine": s.get("engine"),
                           "relevance": s.get("relevance"),
                           "snippet": (s.get("snippet") or "")[:300]})
    rel = [c["relevance"] or 0 for c in comparison]
    agreement = round(sum(rel) / max(1, len(rel)), 3) if rel else 0.0
    return {"comparison": comparison, "agreement": agreement, "claim": claim,
            "source_count": len(comparison)}


# --------------------------------------------------------------------------- #
# WEB_BROWSE
# --------------------------------------------------------------------------- #
@implements("WEB_BROWSE", "olcap-fetch")
def web_browse(params: Dict[str, Any]) -> Dict[str, Any]:
    url = params.get("url") or ""
    if not url:
        raise ValueError("url is required")
    max_bytes = int(params.get("max_bytes", 2_000_000) or 2_000_000)
    with span("web.browse", "capability", {"url": url[:120]}) as sp:
        status, body, final = _fetch(url)
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        if m:
            title = _strip_tags(m.group(1))
        links = []
        for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body[:max_bytes], re.S):
            if href.startswith(("http", "//", "/")):
                links.append({"url": urllib.parse.urljoin(final, _clean_url(href)),
                              "text": _strip_tags(text)[:120]})
            if len(links) >= 100:
                break
        sp.set(status=status, bytes=len(body))
    return {"status": status, "final_url": final, "html": body[:max_bytes],
            "title": title, "links": links, "bytes": len(body)}


# --------------------------------------------------------------------------- #
# WEB_CRAWL
# --------------------------------------------------------------------------- #
_ROBOTS_CACHE: Dict[str, Any] = {}


def _robots_allows(base: str, url: str) -> bool:
    try:
        host = urllib.parse.urlparse(base).netloc
        if host not in _ROBOTS_CACHE:
            try:
                _, body, _ = _fetch(f"{urllib.parse.urljoin(base, '/')}robots.txt",
                                    timeout=10)
                disallow = []
                current = False
                for line in body.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.lower().startswith("user-agent:"):
                        current = line.split(":", 1)[1].strip() in ("*", "olcap")
                    elif line.lower().startswith("disallow:") and current:
                        p = line.split(":", 1)[1].strip()
                        if p:
                            disallow.append(p)
                _ROBOTS_CACHE[host] = disallow
            except Exception:
                _ROBOTS_CACHE[host] = []
        path = urllib.parse.urlparse(url).path or "/"
        for d in _ROBOTS_CACHE[host]:
            if path.startswith(d):
                return False
    except Exception:
        return True
    return True


def _extract_text(body: str) -> str:
    try:
        import trafilatura
        out = trafilatura.extract(body)
        if out:
            return out
    except Exception:
        pass
    return _strip_tags(body)


@implements("WEB_CRAWL", "olcap-crawler")
def web_crawl(params: Dict[str, Any]) -> Dict[str, Any]:
    start = params.get("url") or ""
    max_pages = int(params.get("max_pages", 20) or 20)
    max_depth = int(params.get("max_depth", 2) or 2)
    same_domain = bool(params.get("same_domain", True))
    do_extract = bool(params.get("extract", True))
    if not start:
        raise ValueError("url is required")

    host = urllib.parse.urlparse(start).netloc
    seen, pages, frontier = set(), [], [(start, 0)]
    with span("web.crawl", "capability", {"url": start[:120], "max_pages": max_pages}) as sp:
        while frontier and len(pages) < max_pages:
            url, depth = frontier.pop(0)
            if url in seen or depth > max_depth:
                continue
            seen.add(url)
            if same_domain and urllib.parse.urlparse(url).netloc != host:
                continue
            if not _robots_allows(start, url):
                pages.append({"url": url, "depth": depth, "skipped": "robots.txt"})
                continue
            try:
                status, body, final = _fetch(url, timeout=15)
                if status >= 400:
                    pages.append({"url": url, "depth": depth, "status": status})
                    continue
                text = _extract_text(body) if do_extract else ""
                pages.append({"url": url, "final_url": final, "depth": depth,
                              "status": status, "text": text[:50000],
                              "chars": len(text)})
                if depth < max_depth:
                    for href in re.findall(r'href="([^"#]+)"', body[:400000]):
                        nxt = urllib.parse.urljoin(final, _clean_url(href))
                        if nxt.startswith("http") and nxt not in seen:
                            if same_domain and urllib.parse.urlparse(nxt).netloc != host:
                                continue
                            frontier.append((nxt, depth + 1))
            except Exception as e:
                pages.append({"url": url, "depth": depth,
                              "error": f"{type(e).__name__}: {str(e)[:120]}"})
            time.sleep(0.2)   # be polite
        sp.set(visited=len(pages))
    return {"pages": pages, "visited": len(pages), "requested": max_pages,
            "start_url": start}
