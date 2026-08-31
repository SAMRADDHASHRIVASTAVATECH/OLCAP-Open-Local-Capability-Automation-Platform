"""
MCP SERVER 1 - WEB + BROWSER.

Tools: web_search, web_browse, web_crawl, web_extract, web_interact,
       web_discover_sources, web_compare_sources
       (+ shared core_* tools)
"""
from __future__ import annotations

from .common import build_server, run_server

SERVER_ID = "web_browser"
NAME = "olcap-web-browser"
DESCRIPTION = ("OLCAP MCP Server 1 - Web + Browser: multi-source search, "
               "browsing, crawling, content extraction and browser automation. "
               "Backends are selected automatically with fallback.")


def build():
    # implementations are imported so their @implements decorators register
    from ..core.impls import web as _web  # noqa: F401
    return build_server(SERVER_ID, NAME, DESCRIPTION)


def main() -> None:
    run_server(SERVER_ID, NAME, DESCRIPTION)


if __name__ == "__main__":
    main()
