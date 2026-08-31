"""
Provenance verification.

RULE: never invent a repository, package, license or capability.
Every external component in the catalog is CHECKED against the upstream API:
repository existence, owner, declared license, archived/maintenance state,
last push date. Results are cached on disk with a timestamp.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.config import cfg
from ..core.registry import registry

CACHE = cfg().data / "provenance.json"
UA = "olcap-provenance-verifier"
_GH = re.compile(r"https?://(?:www\.)?github\.com/([^/]+)/([^/#?]+)")


def _gh_slug(url: str) -> Optional[tuple]:
    m = _GH.match(url or "")
    if not m:
        return None
    return m.group(1), m.group(2).removesuffix(".git")


def _get(url: str, timeout: int = 12) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"_error": f"http_{e.code}"}
    except Exception as e:
        return {"_error": type(e).__name__}


def load_cache() -> Dict[str, Any]:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except Exception:
            return {}
    return {}


def save_cache(d: Dict[str, Any]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(d, indent=2))


def verify_component(cid: str, force: bool = False) -> Dict[str, Any]:
    reg = registry()
    comp = reg.component(cid)
    if not comp:
        return {"id": cid, "verified": False, "error": "unknown component"}
    cache = load_cache()
    if not force and cid in cache and time.time() - cache[cid].get("checked_at", 0) < 86400:
        return cache[cid]

    out: Dict[str, Any] = {
        "id": cid, "name": comp.name,
        "declared_repository": comp.repository,
        "declared_license": comp.license,
        "checked_at": time.time(),
    }
    if not comp.repository:
        out.update(verified=True, source="local/builtin", repository_ok=None)
        cache[cid] = out; save_cache(cache)
        return out

    slug = _gh_slug(comp.repository)
    if not slug:
        out.update(verified=False, reason="not a GitHub repository (manual review required)")
        cache[cid] = out; save_cache(cache); return out

    data = _get(f"https://api.github.com/repos/{slug[0]}/{slug[1]}")
    if not data or data.get("_error"):
        out.update(verified=False, reason=f"api error: {data and data.get('_error')}")
        cache[cid] = out; save_cache(cache); return out

    lic = (data.get("license") or {}).get("spdx_id") or "NOASSERTION"
    out.update(
        verified=True,
        repository_ok=True,
        full_name=data.get("full_name"),
        html_url=data.get("html_url"),
        owner=(data.get("owner") or {}).get("login"),
        api_license=lic,
        license_matches=(lic == comp.license or
                         (comp.license or "").startswith(lic) or
                         lic in (comp.license or "") or lic == "NOASSERTION"),
        stars=data.get("stargazers_count"),
        archived=bool(data.get("archived")),
        pushed_at=data.get("pushed_at"),
        open_issues=data.get("open_issues_count"),
        description=(data.get("description") or "")[:200],
        default_branch=data.get("default_branch"),
    )
    pushed = data.get("pushed_at")
    if pushed:
        try:
            t = time.mktime(time.strptime(pushed, "%Y-%m-%dT%H:%M:%SZ"))
            out["days_since_push"] = int((time.time() - t) / 86400)
            out["maintenance_state"] = ("active" if out["days_since_push"] < 180
                                        else "stale" if out["days_since_push"] < 730
                                        else "dormant")
        except Exception:
            pass
    out["acceptable"] = bool(out["repository_ok"] and not out["archived"]
                             and out.get("license_matches"))
    cache[cid] = out
    save_cache(cache)
    return out


def verify_all(force: bool = False, only_external: bool = True) -> Dict[str, Any]:
    reg = registry()
    results: Dict[str, Any] = {}
    for cid, comp in reg.components.items():
        if only_external and not comp.repository:
            continue
        results[cid] = verify_component(cid, force=force)
        time.sleep(0.2)
    summary = {
        "checked": len(results),
        "verified": sum(1 for r in results.values() if r.get("verified")),
        "acceptable": sum(1 for r in results.values() if r.get("acceptable")),
        "license_mismatch": [k for k, r in results.items()
                             if r.get("verified") and not r.get("license_matches")],
        "archived": [k for k, r in results.items() if r.get("archived")],
        "failed": [k for k, r in results.items() if not r.get("verified")],
    }
    return {"summary": summary, "results": results}


if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    only = not ("--all" in sys.argv)
    rep = verify_all(force=force, only_external=only)
    print(json.dumps(rep["summary"], indent=2))
    (cfg().reports).mkdir(parents=True, exist_ok=True)
    (cfg().reports / "provenance.json").write_text(json.dumps(rep, indent=2))
    print(f"written: {cfg().reports/'provenance.json'}")
