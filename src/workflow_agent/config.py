"""Framework constants and path configuration."""

from __future__ import annotations

from pathlib import Path

# Base directory for agent definitions (policies, roles, specs)
AGENTS_DIR = Path(__file__).resolve().parent.parent.parent / "agents"

# Docker image for the agent container
AGENT_IMAGE = "ghcr.io/zzofrea/workflow-agent:latest"

# Host-side Claude auth paths
CLAUDE_AUTH_JSON = str(Path.home() / ".claude.json")
CLAUDE_AUTH_DIR = str(Path.home() / ".claude")

# Container-side paths
CONTAINER_INPUT_DIR = "/agent/input"
CONTAINER_OUTPUT_DIR = "/agent/output"
CONTAINER_AUTH_DIR = "/agent/auth"

# Host-side output archive base
OUTPUT_BASE = str(Path.home() / "agent-output")

# Container defaults
DEFAULT_TIMEOUT = 300
DEFAULT_MAX_TURNS = 50
DEFAULT_MODEL = "sonnet"

# Docker container name prefix
CONTAINER_NAME_PREFIX = "agent"
