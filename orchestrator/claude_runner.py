"""Wrapper for running Claude Code CLI headless."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from llm_runner import LLMRunner, LLMResult

log = logging.getLogger("orchestrator.claude")


# Backward-compatible alias — existing code imports ClaudeResult
ClaudeResult = LLMResult


def _parse_claude_json(data: dict, duration_s: float) -> LLMResult:
    """Parse JSON output from `claude -p --output-format json`."""
    r = LLMResult()
    r.raw = data
    r.duration_s = duration_s
    r.session_id = str(data.get("session_id", ""))
    r.is_error = bool(data.get("is_error", False))
    r.cost_usd = float(data.get("total_cost_usd", 0.0) or data.get("cost_usd", 0.0) or 0.0)
    r.num_turns = int(data.get("num_turns", 0))
    r.model = str(data.get("model", ""))

    result = data.get("result", "")
    if isinstance(result, str):
        r.result_text = result
    elif isinstance(result, list):
        parts = []
        for block in result:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        r.result_text = "\n".join(parts)
    else:
        r.result_text = str(result)

    r.success = not r.is_error and bool(r.result_text)
    return r


# Allowed tools for workers
WORKER_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git status:*)",
    "Bash(git add:*)", "Bash(git commit:*)",
    "Bash(python:*)", "Bash(pytest:*)", "Bash(cargo:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(wc:*)",
]

# Extended tools — includes curl and docker for testing against containers
WORKER_TOOLS_DOCKER = WORKER_TOOLS + [
    "Bash(curl:*)", "Bash(docker exec:*)", "Bash(docker logs:*)",
]

# Infra worker tools — everything a worker has + pip, docker compose, alembic
INFRA_TOOLS = WORKER_TOOLS + [
    "Bash(pip:*)", "Bash(pip3:*)",
    "Bash(docker:*)", "Bash(docker-compose:*)",
    "Bash(alembic:*)",
    "Bash(curl:*)",
    "Bash(mkdir:*)", "Bash(cp:*)", "Bash(mv:*)",
    "Bash(chmod:*)", "Bash(touch:*)",
]

# Researcher tools — web + read, no file writing
RESEARCHER_TOOLS = [
    "ToolSearch", "WebSearch", "WebFetch",
    "Read", "Glob", "Grep",
    "Bash(curl:*)",
]


class ClaudeRunner(LLMRunner):
    def __init__(self, claude_path: str = "claude"):
        self.claude_path = claude_path

    @property
    def supports_tools(self) -> bool:
        return True

    async def run(
        self,
        prompt: str,
        *,
        cwd: str | Path | None = None,
        model: str = "sonnet",
        max_turns: int = 15,
        max_budget_usd: float = 2.0,
        allowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        timeout_s: int | None = None,
    ) -> LLMResult:
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n---\n\n{prompt}"

        args = [
            self.claude_path,
            "-p",
            "--output-format", "json",
            "--model", model,
            "--max-turns", str(max_turns),
        ]

        if allowed_tools:
            args.extend(["--allowedTools", ",".join(allowed_tools)])

        if timeout_s is None:
            timeout_s = max_turns * 120

        log.info("Launching claude: model=%s max_turns=%d budget=$%.2f cwd=%s prompt_len=%d",
                 model, max_turns, max_budget_usd, cwd or ".", len(full_prompt))

        t0 = time.monotonic()
        try:
            prompt_bytes = full_prompt.encode("utf-8")
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=prompt_bytes), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                elapsed = time.monotonic() - t0
                log.error("Claude timeout after %.0fs", elapsed)
                return LLMResult.failure(f"Timeout after {elapsed:.0f}s", elapsed)

            elapsed = time.monotonic() - t0

            raw_out = stdout.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                if not raw_out:
                    log.error("Claude exit code %d: %s", proc.returncode, err[:300] or "(no output)")
                    return LLMResult.failure(f"Exit {proc.returncode}: {err[:500]}", elapsed)
                log.warning("Claude exit code %d but has JSON output, attempting parse", proc.returncode)
            if not raw_out:
                return LLMResult.failure("Empty output", elapsed)

            try:
                data = json.loads(raw_out)
            except json.JSONDecodeError:
                idx = raw_out.rfind('{"type":"result"')
                if idx < 0:
                    idx = raw_out.rfind("{")
                if idx >= 0:
                    try:
                        data = json.loads(raw_out[idx:])
                    except json.JSONDecodeError:
                        return LLMResult.failure(f"Invalid JSON: {raw_out[:300]}", elapsed)
                else:
                    return LLMResult.failure(f"No JSON in output: {raw_out[:300]}", elapsed)

            if isinstance(data, list):
                results = [d for d in data if isinstance(d, dict) and d.get("type") == "result"]
                if results:
                    data = results[-1]
                elif data and isinstance(data[-1], dict):
                    data = data[-1]
                else:
                    return LLMResult.failure(f"JSON list without result: {raw_out[:300]}", elapsed)

            log.debug("Claude raw output type=%s keys=%s", type(data).__name__,
                      list(data.keys())[:10] if isinstance(data, dict) else "N/A")

            result = _parse_claude_json(data, elapsed)
            log.info("Claude completed: %.0fs, $%.4f, %d turns, success=%s",
                     elapsed, result.cost_usd, result.num_turns, result.success)
            return result

        except FileNotFoundError:
            return LLMResult.failure(f"Binary '{self.claude_path}' not found")
        except Exception as e:
            elapsed = time.monotonic() - t0
            log.exception("Unexpected error launching claude")
            return LLMResult.failure(f"Exception: {e}", elapsed)
        finally:
            pass
