"""Entry point: python -m orchestrator"""

import asyncio
import logging
import os
import signal
import sys

from config import Config
from orchestrator import Orchestrator


async def run_with_brief(orch: Orchestrator, brief: str):
    """Brief mode: plan -> research -> execute."""
    await orch._register()
    orch._heartbeat_task = asyncio.create_task(orch._heartbeat_loop())

    # Phase 0: Plan
    plan_result = await orch.plan_project(brief)
    if not plan_result:
        logging.getLogger("orchestrator").error("Planning failed, aborting")
        await orch._cleanup()
        return

    # Phase 1: Research
    research_tasks = plan_result.get("research_tasks", [])
    if research_tasks:
        await orch._run_research_phase(research_tasks)

    # Phase 2-3: Execute (normal loop with tasks already created)
    await orch._main_loop()
    await orch._cleanup()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.from_env()

    log = logging.getLogger("orchestrator")
    log.info("=== Orchestra Orchestrator v%s ===", cfg.version)
    log.info("Console:     %s", cfg.console_url)
    log.info("Frentes:     %s", ", ".join(cfg.frentes) or "(none)")
    log.info("Workers:     max %d", cfg.max_workers)
    log.info("Budget:      $%.2f total, $%.2f/worker, $%.2f/reviewer",
             cfg.max_budget_usd, cfg.worker_budget_usd, cfg.reviewer_budget_usd)
    log.info("LLM backend: %s", cfg.llm_backend)
    if cfg.llm_backend == "ollama":
        log.info("Ollama URL:  %s", cfg.ollama_url)
    log.info("Architect:   %s, Worker: %s, Reviewer: %s",
             cfg.architect_model, cfg.worker_model, cfg.reviewer_model)
    log.info("Worker turns: %d, Lock TTL: %d min", cfg.worker_max_turns, cfg.lock_ttl_min)
    if cfg.integration_branch:
        log.info("Integration: %s (merge pipeline active)", cfg.integration_branch)
    if cfg.docker_target:
        log.info("Docker:      %s (workers can test against it)", cfg.docker_target)
    if cfg.dry_run:
        log.info("*** DRY RUN — no workers will be launched ***")
    log.info("=" * 40)

    orch = Orchestrator(cfg)

    # Signal handlers (Windows-compatible)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, orch.request_shutdown)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda s, f: orch.request_shutdown())

    # Brief mode: BRIEF env var or --brief argument or brief.md in cwd
    # --resume forces normal mode (ignores brief)
    resume_mode = "--resume" in sys.argv
    brief = "" if resume_mode else os.environ.get("BRIEF", "")
    if not brief and not resume_mode and len(sys.argv) > 1 and sys.argv[1] == "--brief":
        brief_path = sys.argv[2] if len(sys.argv) > 2 else "brief.md"
        try:
            with open(brief_path, "r", encoding="utf-8") as f:
                brief = f.read()
            log.info("Brief loaded from %s (%d chars)", brief_path, len(brief))
        except FileNotFoundError:
            log.error("Brief file not found: %s", brief_path)
            sys.exit(1)
    elif not brief and not resume_mode and os.path.exists("brief.md"):
        with open("brief.md", "r", encoding="utf-8") as f:
            brief = f.read()
        log.info("Brief auto-detected: brief.md (%d chars)", len(brief))

    try:
        if brief:
            log.info("=== BRIEF MODE: plan -> research -> execute ===")
            asyncio.run(run_with_brief(orch, brief))
        else:
            log.info("=== NORMAL MODE: execute existing tasks ===")
            asyncio.run(orch.run())
    except KeyboardInterrupt:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
