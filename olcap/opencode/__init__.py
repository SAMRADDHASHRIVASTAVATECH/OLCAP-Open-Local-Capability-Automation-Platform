"""OpenCode integration.

OLCAP is a capability layer *under* OpenCode.  This package never replaces
OpenCode, never rebuilds its SKILL.md mechanism, and never touches its model
routing (OpenCode -> Google API / Groq API).  Its single job is to register the
three OLCAP MCP servers into whatever OpenCode configuration is actually
present on the machine, and to be able to verify and undo that.
"""
from .adapter import (OpenCodeAdapter, find_config, probe, register, verify,
                      rollback, SERVERS)

__all__ = ["OpenCodeAdapter", "find_config", "probe", "register", "verify",
           "rollback", "SERVERS"]
