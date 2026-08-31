"""
Shared data models for the Unified Core, the Dependency Graph and the registries.
Pydantic is used so the same models validate MCP tool input/output schemas.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def new_id(prefix: str = "id") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
class HealthState(str, Enum):
    UNAVAILABLE = "unavailable"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"
    READY = "ready"
    ACTIVE = "active"
    IDLE = "idle"
    RELEASED = "released"


class HealthReport(BaseModel):
    component: str
    state: HealthState = HealthState.UNAVAILABLE
    installed: bool = False
    configured: bool = False
    running: bool = False
    jit_ready: bool = False
    platform_ok: bool = False
    detail: str = ""
    checks: Dict[str, bool] = Field(default_factory=dict)
    latency_ms: float = 0.0
    provenance: str = ""


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
class PermissionCategory(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    CREDENTIALS = "credentials"
    EXTERNAL = "external_communication"
    DESTRUCTIVE = "destructive"


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


# --------------------------------------------------------------------------- #
# Components / capabilities
# --------------------------------------------------------------------------- #
class InstallMethod(str, Enum):
    PYTHON = "python"
    NODE = "node"
    BINARY = "binary"
    DOCKER = "docker"
    SOURCE = "source"
    BUILTIN = "builtin"
    SERVICE = "service"


class ComponentSpec(BaseModel):
    """One installable/usable implementation (backend) of one or more capabilities."""
    id: str
    name: str
    category: str
    capabilities: List[str] = Field(default_factory=list)
    provides: List[str] = Field(default_factory=list)
    repository: str = ""
    documentation: str = ""
    owner: str = ""
    license: str = ""
    version: str = ""
    maintenance: str = ""
    integration: str = "library"          # library | service | cli | mcp
    install_method: InstallMethod = InstallMethod.BUILTIN
    install_target: str = ""              # pip spec | npm spec | url | image
    install_args: List[str] = Field(default_factory=list)
    python_module: str = ""               # import name used to verify presence
    executable: str = ""                  # binary used to verify presence
    healthcheck: str = ""                 # shell command / module callable
    platforms: List[str] = Field(default_factory=lambda: ["windows", "linux"])
    runtimes: List[str] = Field(default_factory=list)
    api_requirements: List[str] = Field(default_factory=list)
    cloud_requirements: List[str] = Field(default_factory=list)
    paid: bool = False
    paid_note: str = ""
    self_hosted: bool = True
    resource_mb: int = 128
    jit: bool = True                      # heavy -> JIT activated
    warm_ttl_s: int = 300
    max_concurrency: int = 2
    permissions: List[PermissionCategory] = Field(default_factory=list)
    fallback_for: List[str] = Field(default_factory=list)
    enabled: bool = True
    optional: bool = False
    notes: str = ""

    def supports(self, platform: str) -> bool:
        return not self.platforms or platform in self.platforms


class CapabilitySpec(BaseModel):
    id: str                                # e.g. WEB_SEARCH
    name: str
    server: str                            # web_browser | research_knowledge | data_automation_os
    tool: str                              # MCP tool name
    category: str
    description: str = ""
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Dict[str, Any] = Field(default_factory=dict)
    implementations: List[str] = Field(default_factory=list)   # component ids, priority order
    fallback: List[str] = Field(default_factory=list)
    # Known components that are NOT wired up: kept for inventory, health and
    # provenance, but never claimed as something this capability can run. A
    # backend only belongs in `implementations` once real code can dispatch to
    # it, otherwise the registry advertises capability it does not have.
    optional_backends: List[str] = Field(default_factory=list)
    permissions: List[PermissionCategory] = Field(default_factory=list)
    platforms: List[str] = Field(default_factory=lambda: ["windows", "linux"])
    jit: bool = True
    verification_required: bool = False


# --------------------------------------------------------------------------- #
# Dependency graph
# --------------------------------------------------------------------------- #
class NodeType(str, Enum):
    GOAL = "goal"
    SUBGOAL = "subgoal"
    TASK = "task"
    SUBTASK = "subtask"
    CAPABILITY = "capability"
    TOOL = "tool"
    SKILL = "skill"
    AGENT = "agent"
    MODEL = "model"
    FILE = "file"
    DOCUMENT = "document"
    KNOWLEDGE = "knowledge"
    DATA = "data"
    API = "api"
    SERVICE = "service"
    PERMISSION = "permission"
    RUNTIME = "runtime"
    WORKFLOW = "workflow"
    PLATFORM = "platform"
    OUTPUT = "output"
    VERIFICATION = "verification"
    WORKER = "worker"


class NodeStatus(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    SUBSTITUTED = "substituted"


class EdgeType(str, Enum):
    DEPENDS_ON = "depends_on"
    OPTIONAL_DEPENDS_ON = "optional_depends_on"
    ALTERNATIVE_OF = "alternative_of"
    SUBSTITUTES = "substitutes"
    CONFLICTS_WITH = "conflicts_with"
    PRODUCES = "produces"
    REQUIRES_CAPABILITY = "requires_capability"
    REQUIRES_PERMISSION = "requires_permission"
    REQUIRES_PLATFORM = "requires_platform"
    REQUIRES_RUNTIME = "requires_runtime"
    REQUIRES_RESOURCE = "requires_resource"
    ASSIGNED_AGENT = "assigned_agent"


class GraphNode(BaseModel):
    id: str = Field(default_factory=lambda: new_id("n"))
    type: NodeType = NodeType.TASK
    title: str = ""
    status: NodeStatus = NodeStatus.PENDING
    capability: Optional[str] = None
    component: Optional[str] = None
    agent: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    result: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3
    critical: bool = False
    objective_id: Optional[str] = None
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


class GraphEdge(BaseModel):
    id: str = Field(default_factory=lambda: new_id("e"))
    src: str                      # dependent
    dst: str                      # dependency
    type: EdgeType = EdgeType.DEPENDS_ON
    optional: bool = False
    weight: float = 1.0
    meta: Dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Agent / execution
# --------------------------------------------------------------------------- #
class AgentRole(str, Enum):
    PLANNER = "planner"
    RESEARCHER = "researcher"
    BROWSER = "browser"
    CODER = "coder"
    TESTER = "tester"
    DEBUGGER = "debugger"
    DOCUMENT = "document"
    DATA = "data"
    SECURITY = "security"
    VERIFIER = "verifier"
    AUTOMATION = "automation"


class TaskResult(BaseModel):
    ok: bool
    node_id: str = ""
    output: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    evidence: List[Dict[str, Any]] = Field(default_factory=list)
    duration_ms: float = 0.0
    component: Optional[str] = None
    provenance: Dict[str, Any] = Field(default_factory=dict)
