"""Abstract base for LLM runners (Claude CLI, Ollama, etc.)."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LLMResult:
    """Unified result from any LLM backend."""
    success: bool = False
    result_text: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    num_turns: int = 0
    session_id: str = ""
    is_error: bool = False
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict = field(default_factory=dict)

    @classmethod
    def failure(cls, error: str, duration_s: float = 0.0) -> "LLMResult":
        return cls(success=False, result_text=error, is_error=True, duration_s=duration_s)


class LLMRunner(ABC):
    """Interface that all LLM backends must implement."""

    @abstractmethod
    async def run(
        self,
        prompt: str,
        *,
        cwd: str | Path | None = None,
        model: str = "",
        max_turns: int = 1,
        max_budget_usd: float = 2.0,
        allowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        timeout_s: int | None = None,
    ) -> LLMResult:
        ...

    @property
    def supports_tools(self) -> bool:
        """Whether this runner can execute tools (Read, Write, Bash, etc.)."""
        return False


def create_runner(backend: str, **kwargs) -> LLMRunner:
    """Factory: instantiate the right runner based on backend name.

    Args:
        backend: "claude" or "ollama"
        **kwargs: passed to the runner constructor
            - claude: claude_path (str)
            - ollama: base_url (str)
    """
    if backend == "ollama":
        from ollama_runner import OllamaRunner
        return OllamaRunner(base_url=kwargs.get("base_url", "http://ollama:11434"))
    else:
        from claude_runner import ClaudeRunner
        return ClaudeRunner(claude_path=kwargs.get("claude_path", "claude"))
