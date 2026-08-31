"""
OLCAP - OpenLive Capability System.

A single capability layer that sits UNDER the existing OpenLive -> OpenCode
stack. OpenLive stays the upper interface, OpenCode stays the execution layer
with its existing SKILL.md mechanism and model routing; OLCAP only adds
capabilities through three grouped MCP servers.
"""
__version__ = "1.0.0"
__all__ = ["__version__"]
