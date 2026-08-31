"""
MCP SERVER 2 - RESEARCH + KNOWLEDGE.

Tools: research_run, knowledge_search, rag_query, memory_op,
       document_process, source_verify
       (+ shared core_* tools)
"""
from __future__ import annotations

from .common import build_server, run_server

SERVER_ID = "research_knowledge"
NAME = "olcap-research-knowledge"
DESCRIPTION = ("OLCAP MCP Server 2 - Research + Knowledge: deep research, "
               "hybrid RAG, shared memory, document intelligence and source "
               "verification. Backends are selected automatically with fallback.")


def build():
    from ..core.impls import knowledge as _knowledge  # noqa: F401
    from ..core.impls import research as _research    # noqa: F401
    return build_server(SERVER_ID, NAME, DESCRIPTION)


def main() -> None:
    run_server(SERVER_ID, NAME, DESCRIPTION)


if __name__ == "__main__":
    main()
