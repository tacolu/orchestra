"""Orchestrator configuration via environment variables."""

import os
import socket
from dataclasses import dataclass, field


@dataclass
class Config:
    console_url: str = ""
    frentes: list[str] = field(default_factory=list)
    max_workers: int = 2
    max_budget_usd: float = 10.0
    worker_budget_usd: float = 2.0
    reviewer_budget_usd: float = 1.0
    architect_budget_usd: float = 1.5
    heartbeat_s: int = 60
    poll_s: int = 30
    lock_ttl_min: int = 45
    claude_path: str = "claude"
    host_name: str = ""
    worker_max_turns: int = 15
    architect_model: str = "opus"
    worker_model: str = "sonnet"
    reviewer_model: str = "sonnet"
    dry_run: bool = False
    version: str = "0.1.0"
    # LLM backend: "claude" (default) or "ollama"
    llm_backend: str = "claude"
    ollama_url: str = "http://ollama:11434"
    ollama_architect_model: str = "llama3.1:70b"
    ollama_worker_model: str = "codellama:34b"
    ollama_reviewer_model: str = "llama3.1:8b"
    # Path mapping Linux -> Windows (prefix:replacement,prefix:replacement)
    path_map: dict[str, str] = field(default_factory=dict)
    # Workspace: directory where repos are cloned automatically
    workspace_dir: str = ""
    # GitHub org for cloning repos (e.g.: myorg)
    github_org: str = ""
    # Mapping frente_slug -> repo_name on GitHub (if different from slug)
    # Format: frente=repo,frente2=repo2
    repo_map: dict[str, str] = field(default_factory=dict)
    # Integration branch where completed tasks are merged (e.g.: feat/feature-name)
    # If empty, each task stays on its branch orch/<code> (isolated)
    integration_branch: str = ""
    # Docker container URL for end-to-end testing (e.g.: http://localhost:8080)
    # Workers can curl against this target if configured
    docker_target: str = ""
    # Architect auto-approve: if false, assignments wait for manual approval in the web chat
    architect_auto_approve: bool = True
    # Max retries per task before requesting human intervention
    max_task_retries: int = 3
    # Cycles without progress before escalation (deadlock detection)
    max_stale_cycles: int = 5

    @classmethod
    def from_env(cls) -> "Config":
        c = cls()
        c.console_url = os.environ.get("CONSOLE_URL", "http://api:8200")
        c.frentes = [
            f.strip()
            for f in os.environ.get("FRENTES", "").split(",")
            if f.strip()
        ]
        c.max_workers = int(os.environ.get("MAX_WORKERS", "2"))
        c.max_budget_usd = float(os.environ.get("MAX_BUDGET_USD", "10.0"))
        c.worker_budget_usd = float(os.environ.get("WORKER_BUDGET_USD", "2.0"))
        c.reviewer_budget_usd = float(os.environ.get("REVIEWER_BUDGET_USD", "1.0"))
        c.architect_budget_usd = float(os.environ.get("ARCHITECT_BUDGET_USD", "1.5"))
        c.heartbeat_s = int(os.environ.get("HEARTBEAT_S", "60"))
        c.poll_s = int(os.environ.get("POLL_S", "30"))
        c.lock_ttl_min = int(os.environ.get("LOCK_TTL_MIN", "45"))
        c.claude_path = os.environ.get("CLAUDE_PATH", "claude")
        c.host_name = os.environ.get("HOST_NAME", socket.gethostname())
        c.worker_max_turns = int(os.environ.get("WORKER_MAX_TURNS", "15"))
        c.architect_model = os.environ.get("ARCHITECT_MODEL", "opus")
        c.worker_model = os.environ.get("WORKER_MODEL", "sonnet")
        c.reviewer_model = os.environ.get("REVIEWER_MODEL", "sonnet")
        c.dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
        c.llm_backend = os.environ.get("LLM_BACKEND", "claude")
        c.ollama_url = os.environ.get("OLLAMA_URL", "http://ollama:11434")
        c.ollama_architect_model = os.environ.get("OLLAMA_ARCHITECT_MODEL", "llama3.1:70b")
        c.ollama_worker_model = os.environ.get("OLLAMA_WORKER_MODEL", "codellama:34b")
        c.ollama_reviewer_model = os.environ.get("OLLAMA_REVIEWER_MODEL", "llama3.1:8b")
        # When using ollama, override model names to ollama model names
        if c.llm_backend == "ollama":
            c.architect_model = c.ollama_architect_model
            c.worker_model = c.ollama_worker_model
            c.reviewer_model = c.ollama_reviewer_model
        c.workspace_dir = os.environ.get("WORKSPACE_DIR", "/workspace")
        c.github_org = os.environ.get("GITHUB_ORG", "")

        raw_repo_map = os.environ.get("REPO_MAP", "")
        if raw_repo_map:
            for pair in raw_repo_map.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    c.repo_map[k.strip()] = v.strip()

        c.integration_branch = os.environ.get("INTEGRATION_BRANCH", "")
        c.docker_target = os.environ.get("DOCKER_TARGET", "")
        c.architect_auto_approve = os.environ.get("ARCHITECT_AUTO_APPROVE", "true").lower() in ("1", "true", "yes")
        c.max_task_retries = int(os.environ.get("MAX_TASK_RETRIES", "3"))
        c.max_stale_cycles = int(os.environ.get("MAX_STALE_CYCLES", "5"))

        raw_map = os.environ.get("PATH_MAP", "")
        if raw_map:
            for pair in raw_map.split(","):
                if ":" in pair:
                    parts = pair.split("=", 1)
                    if len(parts) == 2:
                        c.path_map[parts[0].strip()] = parts[1].strip()
        return c

    def resolve_path(self, path: str | None) -> str | None:
        """Apply path_map to convert paths between environments."""
        if not path:
            return path
        for prefix, replacement in self.path_map.items():
            if path.startswith(prefix):
                return replacement + path[len(prefix):]
        return path
