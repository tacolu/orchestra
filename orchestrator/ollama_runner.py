"""Ollama LLM runner — HTTP client against the Ollama API.

Ollama runners generate text only. They cannot execute tools (Read, Write, Bash).
Use for architect/reviewer roles or plan-only mode. Workers need Claude CLI
for actual code editing.

API docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""

import logging
import time
from pathlib import Path

import httpx

from llm_runner import LLMRunner, LLMResult

log = logging.getLogger("orchestrator.ollama")


class OllamaRunner(LLMRunner):
    def __init__(self, base_url: str = "http://ollama:11434"):
        self.base_url = base_url.rstrip("/")

    @property
    def supports_tools(self) -> bool:
        return False

    async def run(
        self,
        prompt: str,
        *,
        cwd: str | Path | None = None,
        model: str = "llama3.1:70b",
        max_turns: int = 1,
        max_budget_usd: float = 2.0,
        allowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        timeout_s: int | None = None,
    ) -> LLMResult:
        if timeout_s is None:
            timeout_s = 300  # 5 min default for local models

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": 4096,
            },
        }

        log.info("Launching ollama: model=%s url=%s prompt_len=%d",
                 model, self.base_url, len(prompt))

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    json=body,
                )

            elapsed = time.monotonic() - t0

            if resp.status_code != 200:
                error_text = resp.text[:500]
                log.error("Ollama HTTP %d: %s", resp.status_code, error_text)
                return LLMResult.failure(
                    f"Ollama HTTP {resp.status_code}: {error_text}", elapsed
                )

            data = resp.json()
            message = data.get("message", {})
            content = message.get("content", "")

            input_tokens = data.get("prompt_eval_count", 0)
            output_tokens = data.get("eval_count", 0)

            result = LLMResult(
                success=bool(content),
                result_text=content,
                cost_usd=0.0,
                duration_s=elapsed,
                num_turns=1,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw=data,
            )

            log.info("Ollama completed: %.1fs, %d in / %d out tokens, model=%s",
                     elapsed, input_tokens, output_tokens, model)
            return result

        except httpx.TimeoutException:
            elapsed = time.monotonic() - t0
            log.error("Ollama timeout after %.0fs", elapsed)
            return LLMResult.failure(f"Timeout after {elapsed:.0f}s", elapsed)
        except httpx.ConnectError as e:
            elapsed = time.monotonic() - t0
            log.error("Ollama connection failed: %s", e)
            return LLMResult.failure(f"Connection failed: {e}", elapsed)
        except Exception as e:
            elapsed = time.monotonic() - t0
            log.exception("Unexpected error calling ollama")
            return LLMResult.failure(f"Exception: {e}", elapsed)
