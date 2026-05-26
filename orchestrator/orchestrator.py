"""Main orchestrator loop."""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from config import Config
from console_client import ConsoleClient, ConsoleAPIError
from llm_runner import LLMResult, create_runner
from claude_runner import ClaudeResult, WORKER_TOOLS, WORKER_TOOLS_DOCKER, INFRA_TOOLS, RESEARCHER_TOOLS
from roles import (
    build_architect_prompt, build_worker_prompt, build_reviewer_prompt,
    build_planner_prompt, build_researcher_prompt,
    build_infra_worker_prompt, build_validation_prompt,
)
from shared_context import SharedContext
from postmortem import PostMortemAnalyzer, TaskAttempt, FailureType, AdjustmentType
from preflight import PreflightChecker, PreflightResult

log = logging.getLogger("orchestrator")


@dataclass
class WorkerState:
    """State of an in-flight worker."""
    tarea_codigo: str
    task_data: dict
    lock_id: int
    asyncio_task: asyncio.Task
    skills_content: str = ""
    started_at: float = 0.0
    head_before: str = ""
    branch_name: str = ""
    cwd: str = ""


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.console = ConsoleClient(cfg.console_url)
        self.runner = create_runner(
            cfg.llm_backend,
            claude_path=cfg.claude_path,
            base_url=cfg.ollama_url,
        )
        self.session_id: int | None = None
        self.session_name: str = ""
        self.total_spent: float = 0.0
        self.tasks_completed: int = 0
        self.tasks_failed: int = 0
        self.active_workers: dict[str, WorkerState] = {}
        self._shutdown = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None
        db_dir = cfg.workspace_dir or "."
        self.shared_ctx = SharedContext(os.path.join(db_dir, "shared_context.db"))
        self.postmortem = PostMortemAnalyzer()
        self.postmortem.hydrate(self.shared_ctx.get_failure_history())
        self._round_attempts: list[TaskAttempt] = []
        self.preflight = PreflightChecker(
            workspace_dir=cfg.workspace_dir,
            github_org=cfg.github_org,
            repo_map=cfg.repo_map,
        )
        self._last_architect_workers: int = 0
        self._harvest_produced: bool = False
        self._stale_cycles: int = 0
        self._last_completed_count: int = 0
        self._human_intervention_requested: bool = False

    def request_shutdown(self):
        log.info("Shutdown requested")
        self._shutdown.set()

    # == LIFECYCLE ==

    async def run(self):
        try:
            await self._register()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            await self._main_loop()
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt")
        except Exception:
            log.exception("Fatal error in orchestrator")
        finally:
            await self._cleanup()

    async def _register(self):
        import time as _time
        ts = int(_time.time()) % 100000
        self.session_name = f"orch-{self.cfg.host_name}-{ts}"
        frente = self.cfg.frentes[0] if self.cfg.frentes else "default"
        resp = await self.console.register_session(
            nombre=self.session_name,
            frente=frente,
            host=self.cfg.host_name,
            pid=os.getpid(),
        )
        self.session_id = resp["id"]
        log.info("Session registered: id=%d name=%s front=%s",
                 self.session_id, self.session_name, frente)

        await self.console.emit_event(
            f"Orchestrator started: max_workers={self.cfg.max_workers}, "
            f"budget=${self.cfg.max_budget_usd}, fronts={','.join(self.cfg.frentes)}",
            frente=frente,
            sesion_id=self.session_id,
        )

    async def _heartbeat_loop(self):
        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(self.cfg.heartbeat_s)
                if self.session_id:
                    await self.console.heartbeat(self.session_id, self.cfg.lock_ttl_min)
                    log.debug("Heartbeat OK")
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("Heartbeat failed: %s", e)

    async def _cleanup(self):
        log.info("Cleanup: %d active workers", len(self.active_workers))

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self.active_workers:
            log.info("Waiting for %d workers (max 60s)...", len(self.active_workers))
            pending = [w.asyncio_task for w in self.active_workers.values()]
            done, not_done = await asyncio.wait(pending, timeout=60)
            for task in not_done:
                task.cancel()
            for codigo, ws in self.active_workers.items():
                if ws.asyncio_task in not_done:
                    try:
                        await self.console.patch_task(codigo, estado="review")
                        log.info("Task %s moved to review on shutdown (will be re-assigned)", codigo)
                    except Exception:
                        pass

        if self.session_id:
            try:
                locks = await self.console.list_locks(sesion_id=self.session_id)
                for lock in locks:
                    try:
                        await self.console.release_lock(lock["lock_id"], self.session_id)
                        log.info("Lock %d released (task %s)", lock["lock_id"], lock.get("tarea_codigo"))
                    except Exception as e:
                        log.warning("Error releasing lock %d: %s", lock["lock_id"], e)
            except Exception as e:
                log.warning("Error listing locks: %s", e)

        if self.session_id:
            pm_stats = self.postmortem.get_stats()
            summary = (
                f"Orchestrator closed. Completed: {self.tasks_completed}, "
                f"failed: {self.tasks_failed}, total cost: ${self.total_spent:.4f}. "
                f"PostMortem: {pm_stats['rounds']} rounds, {pm_stats['total_attempts']} attempts, "
                f"types: {pm_stats['by_type']}"
            )
            try:
                await self.console.close_session(
                    self.session_id, "shutdown", nota_diario=summary
                )
                log.info(summary)
            except Exception as e:
                log.warning("Error closing session: %s", e)

        await self.console.close()

    # == MAIN LOOP ==

    async def _main_loop(self):
        log.info("Main loop started (poll every %ds)", self.cfg.poll_s)

        while not self._shutdown.is_set():
            try:
                await self._harvest()

                if not self.active_workers and self._round_attempts:
                    await self._run_postmortem()

                if await self._check_deadlock():
                    break

                if self.total_spent >= self.cfg.max_budget_usd:
                    log.warning("Budget exhausted ($%.2f/$%.2f). Waiting for active workers...",
                                self.total_spent, self.cfg.max_budget_usd)
                    if not self.active_workers:
                        log.info("No active workers and no budget. Shutdown.")
                        break
                    await asyncio.sleep(self.cfg.poll_s)
                    continue

                slots = self.cfg.max_workers - len(self.active_workers)
                remaining_budget = self.cfg.max_budget_usd - self.total_spent
                if slots > 0 and remaining_budget >= self.cfg.worker_budget_usd:
                    if self.active_workers and not self._harvest_produced:
                        log.debug("Skip architect: active workers and nothing changed")
                    else:
                        await self._architect_cycle(slots)
                elif slots > 0 and remaining_budget < self.cfg.worker_budget_usd:
                    if not self.active_workers:
                        log.warning("Insufficient budget ($%.2f < $%.2f/worker) and no active workers. Shutdown.",
                                    remaining_budget, self.cfg.worker_budget_usd)
                        break
                    log.info("Skip architect: insufficient budget ($%.2f < $%.2f/worker), waiting for active workers",
                              remaining_budget, self.cfg.worker_budget_usd)

            except asyncio.CancelledError:
                break
            except ConnectionError as e:
                log.error("API unreachable: %s. Pausing 60s.", e)
                await asyncio.sleep(60)
            except Exception:
                log.exception("Error in main cycle")
                await asyncio.sleep(self.cfg.poll_s)

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=self.cfg.poll_s)
                break
            except asyncio.TimeoutError:
                pass

    # == POSTMORTEM ==

    async def _run_postmortem(self):
        if not self._round_attempts:
            return

        adjustments = self.postmortem.analyze_round(self._round_attempts)
        applied = 0

        for adj in adjustments:
            if adj.auto_apply:
                success = await self._apply_adjustment(adj)
                if success:
                    applied += 1

        if applied:
            await self.console.emit_event(
                f"PostMortem round {self.postmortem.round_number}: "
                f"{applied}/{len(adjustments)} adjustments applied automatically",
                sesion_id=self.session_id,
                payload={"stats": self.postmortem.get_stats()},
            )

        self._round_attempts = []

    async def _apply_adjustment(self, adj) -> bool:
        try:
            if adj.tipo == AdjustmentType.EXPAND_SCOPE:
                task = await self.console.get_task(adj.tarea_codigo)
                current_scope = task.get("archivos_glob", "")
                new_scope = current_scope
                if adj.new_scope:
                    existing = set(g.strip() for g in current_scope.split(",") if g.strip())
                    new_dirs = set(g.strip() for g in adj.new_scope.split(",") if g.strip())
                    merged = existing | new_dirs
                    new_scope = ",".join(sorted(merged))
                await self.console.patch_task(adj.tarea_codigo, archivos_glob=new_scope)
                log.info("PostMortem EXPAND_SCOPE %s: %s -> %s", adj.tarea_codigo, current_scope, new_scope)
                await self.console.emit_event(
                    f"Auto-expand scope {adj.tarea_codigo}: {adj.description}",
                    tarea_codigo=adj.tarea_codigo, sesion_id=self.session_id,
                )
                return True

            elif adj.tipo == AdjustmentType.ADD_READ_CONTEXT:
                task = await self.console.get_task(adj.tarea_codigo)
                current_rc = task.get("read_context", "")
                if adj.read_context:
                    existing = set(p.strip() for p in current_rc.split(",") if p.strip())
                    new_paths = set(p.strip() for p in adj.read_context.split(",") if p.strip())
                    merged = existing | new_paths
                    new_rc = ",".join(sorted(merged))
                    await self.console.patch_task(adj.tarea_codigo, read_context=new_rc)
                    log.info("PostMortem ADD_READ_CONTEXT %s: %s", adj.tarea_codigo, new_rc)
                    await self.console.emit_event(
                        f"Auto-add read_context {adj.tarea_codigo}: {adj.description}",
                        tarea_codigo=adj.tarea_codigo, sesion_id=self.session_id,
                    )
                return True

            elif adj.tipo == AdjustmentType.INJECT_CONSTRAINT:
                self.shared_ctx.add_decision(
                    tarea_codigo=adj.tarea_codigo,
                    tipo="review_rejection",
                    contenido=f"AUTO-CONSTRAINT: {adj.constraint}",
                    tags=["postmortem-auto"],
                )
                log.info("PostMortem INJECT_CONSTRAINT %s: %s", adj.tarea_codigo, adj.constraint[:100])
                return True

            elif adj.tipo == AdjustmentType.REQUEST_SPLIT:
                await self.console.emit_event(
                    f"SPLIT SUGGESTED for {adj.tarea_codigo}: {adj.description}",
                    tarea_codigo=adj.tarea_codigo, sesion_id=self.session_id,
                )
                log.warning("PostMortem REQUEST_SPLIT %s: %s", adj.tarea_codigo, adj.description)
                return True

            elif adj.tipo == AdjustmentType.MARK_NEEDS_HUMAN:
                try:
                    await self.console.patch_task(adj.tarea_codigo, estado="blocked")
                except Exception:
                    pass
                await self.console.emit_event(
                    f"NEEDS HUMAN: {adj.tarea_codigo} after {adj.description}",
                    tarea_codigo=adj.tarea_codigo, sesion_id=self.session_id,
                )
                log.warning("PostMortem NEEDS_HUMAN %s: %s", adj.tarea_codigo, adj.description)
                return True

        except Exception as e:
            log.warning("PostMortem failed applying %s for %s: %s", adj.tipo.value, adj.tarea_codigo, e)
            return False

        return False

    # == DEADLOCK DETECTION ==

    async def _check_deadlock(self) -> bool:
        if self.active_workers:
            self._stale_cycles = 0
            return False

        if self.tasks_completed > self._last_completed_count:
            self._last_completed_count = self.tasks_completed
            self._stale_cycles = 0
            return False

        self._stale_cycles += 1

        if self._stale_cycles < self.cfg.max_stale_cycles:
            return False

        log.warning("DEADLOCK: %d cycles without progress (completed=%d, failed=%d)",
                    self._stale_cycles, self.tasks_completed, self.tasks_failed)

        recovered = await self._recover_blocked_tasks()
        if recovered:
            log.info("Recovery: %d tasks unblocked for retry", recovered)
            self._stale_cycles = 0
            return False

        if not self._human_intervention_requested:
            await self._request_human_intervention()
            self._human_intervention_requested = True
            self._stale_cycles = 0
            return False

        log.error("SHUTDOWN: human intervention requested but no progress. Closing.")
        await self.console.emit_event(
            "SHUTDOWN: unrecoverable deadlock after requesting human intervention. "
            f"Completed: {self.tasks_completed}, failed: {self.tasks_failed}",
            sesion_id=self.session_id,
        )
        return True

    async def _recover_blocked_tasks(self) -> int:
        recovered = 0
        for frente in self.cfg.frentes:
            try:
                blocked_tasks = await self.console.get_tasks_by_estado(frente, "blocked")
                review_tasks = await self.console.get_tasks_by_estado(frente, "review")

                for task in blocked_tasks + review_tasks:
                    codigo = task.get("codigo", "")
                    if not codigo:
                        continue

                    rejection_count = self.shared_ctx.count_rejections(codigo)
                    max_allowed = self.cfg.max_task_retries + 1
                    if rejection_count < max_allowed:
                        try:
                            estado = task.get("estado", "")
                            if estado == "blocked":
                                await self.console.patch_task(codigo, estado="in_progress")
                            recovered += 1
                            log.info("Recovery: task %s unblocked (%d/%d retries)",
                                     codigo, rejection_count, self.cfg.max_task_retries)
                            await self.console.emit_event(
                                f"Recovery: {codigo} unblocked for retry "
                                f"({rejection_count}/{self.cfg.max_task_retries})",
                                tarea_codigo=codigo, sesion_id=self.session_id,
                            )
                        except Exception as e:
                            log.debug("Recovery: could not transition %s: %s", codigo, e)

            except Exception as e:
                log.debug("Recovery: error in front %s: %s", frente, e)

        return recovered

    async def _request_human_intervention(self):
        diagnostico_parts = [
            f"Cycles without progress: {self._stale_cycles}",
            f"Tasks completed: {self.tasks_completed}",
            f"Tasks failed: {self.tasks_failed}",
            f"Budget spent: ${self.total_spent:.2f}/{self.cfg.max_budget_usd:.2f}",
        ]

        for frente in self.cfg.frentes:
            try:
                for estado in ("blocked", "review", "in_progress", "available"):
                    tasks = await self.console.get_tasks_by_estado(frente, estado)
                    if tasks:
                        codigos = [t.get("codigo", "?") for t in tasks[:5]]
                        diagnostico_parts.append(f"  {estado}: {', '.join(codigos)}")
                        if estado == "blocked":
                            for t in tasks[:3]:
                                c = t.get("codigo", "")
                                rejs = self.shared_ctx.count_rejections(c)
                                diagnostico_parts.append(f"    {c}: {rejs} rejections")
            except Exception:
                pass

        diagnostico = "\n".join(diagnostico_parts)
        log.error("HUMAN INTERVENTION REQUIRED:\n%s", diagnostico)

        await self.console.emit_event(
            f"HUMAN INTERVENTION REQUIRED — deadlock detected.\n{diagnostico}",
            sesion_id=self.session_id,
        )

    # == HARVEST ==

    async def _harvest(self):
        finished = [
            (code, ws) for code, ws in self.active_workers.items()
            if ws.asyncio_task.done()
        ]
        self._harvest_produced = len(finished) > 0
        for codigo, ws in finished:
            del self.active_workers[codigo]
            try:
                result: ClaudeResult = ws.asyncio_task.result()
            except Exception as e:
                log.error("Worker %s crashed: %s", codigo, e)
                await self._handle_worker_failure(ws, f"Crash: {e}")
                continue

            self.total_spent += result.cost_usd
            log.info("Worker %s finished: success=%s, $%.4f, %.0fs, %d turns",
                     codigo, result.success, result.cost_usd, result.duration_s,
                     result.num_turns)

            if not result.success:
                if result.num_turns >= self.cfg.worker_max_turns and ws.cwd and ws.head_before:
                    has_diff = await self._worker_has_diff(ws)
                    if has_diff:
                        log.info("Worker %s exhausted turns but has diff — attempting review", codigo)
                        self._round_attempts.append(TaskAttempt(
                            tarea_codigo=codigo,
                            failure_type=FailureType.TURN_EXHAUSTION_WITH_DIFF,
                            turns_used=result.num_turns, cost_usd=result.cost_usd,
                        ))
                        self.postmortem.record(self._round_attempts[-1])
                        parsed = self._parse_worker_result(result.result_text)
                        if not parsed.get("status"):
                            parsed = {"status": "done", "summary": f"Exhausted {result.num_turns} turns, partial diff", "files_changed": []}
                        await self._handle_worker_done(ws, result, parsed)
                        continue
                    else:
                        log.warning("Worker %s exhausted %d turns without producing diff — treating as BLOCKED",
                                    codigo, result.num_turns)
                        self._round_attempts.append(TaskAttempt(
                            tarea_codigo=codigo,
                            failure_type=FailureType.TURN_EXHAUSTION_NO_DIFF,
                            turns_used=result.num_turns, cost_usd=result.cost_usd,
                        ))
                        self.postmortem.record(self._round_attempts[-1])
                        await self._handle_worker_blocked(ws, {
                            "blocker": f"Exhausted {result.num_turns} turns without committing. Probable exploration loop."
                        })
                        continue
                await self._handle_worker_failure(ws, result.result_text)
                continue

            parsed = self._parse_worker_result(result.result_text)

            if parsed.get("status") == "done":
                await self._handle_worker_done(ws, result, parsed)
            elif parsed.get("status") == "blocked":
                await self._handle_worker_blocked(ws, parsed)
            else:
                await self._handle_worker_failure(ws, parsed.get("summary", "Unknown status"))

    def _parse_worker_result(self, text: str) -> dict:
        match = re.search(r'ORCHESTRATOR_RESULT::(\{.*\})', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return {"status": "done", "summary": text[-500:], "files_changed": []}

    async def _handle_worker_done(self, ws: WorkerState, result: ClaudeResult, parsed: dict):
        codigo = ws.tarea_codigo
        summary = parsed.get("summary", "")
        frente_slug = ws.task_data.get("frente_slug", "")

        decisions = parsed.get("decisions", [])
        if decisions:
            for d in decisions:
                d["tarea_codigo"] = codigo
                d["frente"] = frente_slug
            count = self.shared_ctx.add_many(decisions)
            log.info("Worker %s reported %d decisions", codigo, count)

        if self.cfg.dry_run:
            log.info("[DRY RUN] Worker %s done: %s", codigo, summary)
            await self._release_and_complete(ws)
            return

        cwd = ws.cwd
        git_diff = ""
        if cwd and ws.head_before:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "diff", f"{ws.head_before}..HEAD",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                git_diff = stdout.decode("utf-8", errors="replace")
            except Exception as e:
                log.warning("Could not get git diff for %s: %s", codigo, e)

        is_infra = self._is_infra_task(ws.task_data, "")
        if git_diff and not is_infra:
            archivos_glob = ws.task_data.get("archivos_glob", "")
            out_of_scope = self._check_scope(git_diff, archivos_glob)
            if out_of_scope:
                violation_msg = (
                    f"Worker touched files out of scope: {', '.join(out_of_scope[:5])}. "
                    f"Allowed scope: {archivos_glob}"
                )
                log.warning("SCOPE VIOLATION %s: %s", codigo, violation_msg)
                await self.console.emit_event(
                    f"Scope violation in {codigo}: {violation_msg}",
                    tarea_codigo=codigo, sesion_id=self.session_id,
                )
                self.shared_ctx.add_decision(
                    tarea_codigo=codigo,
                    tipo="review_rejection",
                    contenido=f"SCOPE VIOLATION: {violation_msg}",
                    frente=frente_slug,
                    tags=["auto-scope-check"],
                )
                rejection_count = self.shared_ctx.count_rejections(codigo)
                if rejection_count >= 3:
                    log.error("Task %s rejected %d times (scope) — escalating", codigo, rejection_count)
                    await self.console.emit_event(
                        f"ESCALATION: {codigo} rejected {rejection_count} times for scope. Requires intervention.",
                        tarea_codigo=codigo, sesion_id=self.session_id,
                    )
                self._round_attempts.append(TaskAttempt(
                    tarea_codigo=codigo,
                    failure_type=FailureType.SCOPE_VIOLATION,
                    detail=violation_msg,
                    out_of_scope_files=out_of_scope,
                ))
                self.postmortem.record(self._round_attempts[-1])
                await self._release_and_return(ws)
                self.tasks_failed += 1
                return

        if git_diff and is_infra:
            log.info("Infra task %s: skip reviewer, running validation gate", codigo)
            attempt = TaskAttempt(tarea_codigo=codigo, failure_type=FailureType.COMPLETED)
            self._round_attempts.append(attempt)
            self.postmortem.record(attempt)
            validation = await self._run_validation_gate(cwd, frente_slug)
            if validation and validation.get("status") == "fail":
                log.warning("Validation gate failed for infra %s — retrying", codigo)
                await self._release_and_return(ws)
                self.tasks_failed += 1
                return
            self._save_file_inventory(codigo, frente_slug, git_diff, summary)
            await self._merge_to_integration(ws)
            await self._release_and_complete(ws)
            return

        shared_decisions = self.shared_ctx.format_for_prompt(frente=frente_slug)

        if git_diff:
            review = await self._run_reviewer(ws, git_diff, summary, shared_decisions)
            self.total_spent += review.cost_usd

            review_parsed = self._parse_reviewer_result(review.result_text)

            if review_parsed.get("verdict") == "approve":
                log.info("Reviewer approves %s: %s", codigo, review_parsed.get("summary", ""))
                await self.console.emit_event(
                    f"Reviewer approves {codigo}: {review_parsed.get('summary', '')}",
                    tarea_codigo=codigo, sesion_id=self.session_id,
                )
                attempt = TaskAttempt(tarea_codigo=codigo, failure_type=FailureType.COMPLETED)
                self._round_attempts.append(attempt)
                self.postmortem.record(attempt)
                self._save_file_inventory(codigo, frente_slug, git_diff, summary)
                if self._is_infra_task(ws.task_data, ""):
                    validation = await self._run_validation_gate(ws.cwd, frente_slug)
                    if validation and validation.get("status") == "fail":
                        log.warning("Validation gate failed for %s — task completed but system doesn't start", codigo)
                await self._merge_to_integration(ws)
                await self._release_and_complete(ws)
            else:
                issues = review_parsed.get("issues", [])
                log.warning("Reviewer rejects %s: %s", codigo, issues)
                await self.console.emit_event(
                    f"Reviewer rejects {codigo}: {'; '.join(issues[:3])}",
                    tarea_codigo=codigo, sesion_id=self.session_id,
                )
                rejection_text = "; ".join(issues[:5])
                self.shared_ctx.add_decision(
                    tarea_codigo=codigo,
                    tipo="review_rejection",
                    contenido=rejection_text,
                    frente=frente_slug,
                    tags=["auto-review"],
                )
                self._round_attempts.append(TaskAttempt(
                    tarea_codigo=codigo,
                    failure_type=FailureType.REVIEWER_REJECTION,
                    detail=rejection_text,
                    rejection_issues=issues[:5],
                ))
                self.postmortem.record(self._round_attempts[-1])
                rejection_count = self.shared_ctx.count_rejections(codigo)
                if rejection_count >= 3:
                    log.error("Task %s rejected %d times — marking as blocked", codigo, rejection_count)
                    await self.console.emit_event(
                        f"ESCALATION: {codigo} rejected {rejection_count} times. Requires intervention.",
                        tarea_codigo=codigo, sesion_id=self.session_id,
                    )
                    try:
                        await self.console.release_lock(ws.lock_id, self.session_id)
                    except Exception:
                        pass
                    try:
                        await self.console.patch_task(codigo, estado="review")
                        await self.console.patch_task(codigo, estado="in_progress")
                        await self.console.patch_task(codigo, estado="review")
                    except Exception:
                        pass
                    self.tasks_failed += 1
                else:
                    await self._release_and_return(ws)
        else:
            archivos_glob = ws.task_data.get("archivos_glob", "")
            if archivos_glob and ws.cwd:
                log.warning("Worker %s produced no diff but task requires files (%s) — treating as failure",
                            codigo, archivos_glob[:60])
                self._round_attempts.append(TaskAttempt(
                    tarea_codigo=codigo, failure_type=FailureType.WORKER_CRASH,
                    detail="Produced no diff despite having archivos_glob defined",
                ))
                self.postmortem.record(self._round_attempts[-1])
                await self._release_and_return(ws)
                self.tasks_failed += 1
            elif not ws.cwd:
                log.error("Worker %s completed without cwd — result discarded", codigo)
                self._round_attempts.append(TaskAttempt(
                    tarea_codigo=codigo, failure_type=FailureType.WORKER_CRASH,
                    detail="Worker executed without cwd (no repo)",
                ))
                self.postmortem.record(self._round_attempts[-1])
                await self._release_and_return(ws)
                self.tasks_failed += 1
            else:
                log.info("No diff for %s (task without archivos_glob), approving", codigo)
                attempt = TaskAttempt(tarea_codigo=codigo, failure_type=FailureType.COMPLETED)
                self._round_attempts.append(attempt)
                self.postmortem.record(attempt)
                await self._release_and_complete(ws)

    def _check_scope(self, git_diff: str, archivos_glob: str | None) -> list[str]:
        if not archivos_glob:
            return []

        diff_files = set()
        for m in re.finditer(r"^diff --git a/(.+?) b/(.+?)$", git_diff, re.MULTILINE):
            diff_files.add(m.group(2))

        if not diff_files:
            return []

        allowed_dirs = set()
        for g in archivos_glob.split(","):
            g = g.strip()
            if not g:
                continue
            parent = os.path.dirname(g)
            allowed_dirs.add(parent if parent else ".")

        expanded = set()
        for d in allowed_dirs:
            if d.startswith("app/models"):
                expanded.add("alembic")
                expanded.add("alembic/versions")
            if d.startswith("app/routers"):
                expanded.add("app/schemas")
                expanded.add("app/services")
                expanded.add("app")
            if d.startswith("app/tests") or d.startswith("tests"):
                expanded.add("app/tests")
                expanded.add("tests")
        allowed_dirs.update(expanded)

        if not allowed_dirs:
            return []

        out_of_scope = []
        for fpath in sorted(diff_files):
            fdir = os.path.dirname(fpath) if os.path.dirname(fpath) else "."
            matched = any(
                fdir == adir or fdir.startswith(adir + "/")
                for adir in allowed_dirs
            )
            if not matched:
                out_of_scope.append(fpath)

        return out_of_scope

    def _save_file_inventory(self, codigo: str, frente: str, git_diff: str, summary: str):
        files = []
        for m in re.finditer(r"^diff --git a/(.+?) b/(.+?)$", git_diff, re.MULTILINE):
            files.append(m.group(2))

        if not files:
            return

        inventory = f"Files: {', '.join(files[:10])}. Summary: {summary[:200]}"
        self.shared_ctx.add_decision(
            tarea_codigo=codigo,
            tipo="file_inventory",
            contenido=inventory,
            frente=frente,
            tags=["auto-inventory"],
        )
        log.info("File inventory saved for %s: %d files", codigo, len(files))

    async def _worker_has_diff(self, ws: WorkerState) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--stat", f"{ws.head_before}..HEAD",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=ws.cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return bool(stdout.decode().strip())
        except Exception:
            return False

    async def _preprocess_repo_context(self, cwd: str, task_data: dict) -> str:
        sections = []
        archivos_glob = task_data.get("archivos_glob", "")

        # 1. File tree
        try:
            proc = await asyncio.create_subprocess_exec(
                "find", ".", "-name", "*.py", "-not", "-path", "./.venv/*",
                "-not", "-path", "./__pycache__/*",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            tree = stdout.decode("utf-8", errors="replace").strip()
            if tree:
                lines = tree.split("\n")[:60]
                sections.append(f"## FILES IN REPO ({len(lines)} .py)\n```\n" + "\n".join(lines) + "\n```")
        except Exception:
            pass

        # 2. Sniffer: alembic head
        if archivos_glob and any(k in archivos_glob for k in ("alembic", "models", "migration")):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "python", "-m", "alembic", "heads",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
                heads_out = stdout.decode("utf-8", errors="replace").strip()
                if heads_out and proc.returncode == 0:
                    sections.append(f"## CURRENT ALEMBIC HEAD\n```\n{heads_out}\n```\n"
                                    "IMPORTANT: your migration MUST use this revision as down_revision.")
                else:
                    proc2 = await asyncio.create_subprocess_exec(
                        "find", ".", "-path", "*/versions/*.py", "-not", "-name", "__pycache__",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                        cwd=cwd,
                    )
                    stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5)
                    migration_files = stdout2.decode("utf-8", errors="replace").strip()
                    if migration_files:
                        last_migration = sorted(migration_files.split("\n"))[-1]
                        try:
                            full_path = os.path.join(cwd, last_migration.lstrip("./"))
                            with open(full_path, "r", encoding="utf-8") as f:
                                content = f.read(500)
                            rev_match = re.search(r"revision\s*=\s*['\"]([^'\"]+)['\"]", content)
                            if rev_match:
                                sections.append(
                                    f"## LAST ALEMBIC MIGRATION\n"
                                    f"File: `{last_migration}`\n"
                                    f"revision = '{rev_match.group(1)}'\n\n"
                                    f"IMPORTANT: your migration MUST use down_revision = '{rev_match.group(1)}'")
                        except Exception:
                            pass
            except Exception:
                pass

        # 3. Read existing files in task scope
        if archivos_glob and cwd:
            existing_files = []
            for g in archivos_glob.split(","):
                g = g.strip()
                if not g:
                    continue
                parent_dir = os.path.dirname(g)
                if not parent_dir:
                    parent_dir = "."
                full_dir = os.path.join(cwd, parent_dir)
                if not os.path.isdir(full_dir):
                    continue
                try:
                    for fname in sorted(os.listdir(full_dir)):
                        if fname.startswith("__") or not fname.endswith(".py"):
                            continue
                        fpath = os.path.join(full_dir, fname)
                        if os.path.isfile(fpath):
                            existing_files.append((os.path.join(parent_dir, fname), fpath))
                except Exception:
                    pass

            if existing_files:
                content_parts = []
                total_chars = 0
                max_chars = 4000
                for rel_path, abs_path in existing_files[:5]:
                    try:
                        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read(max_chars - total_chars)
                        if content.strip():
                            content_parts.append(f"### {rel_path}\n```python\n{content}\n```")
                            total_chars += len(content)
                            if total_chars >= max_chars:
                                break
                    except Exception:
                        pass
                if content_parts:
                    sections.append(
                        "## EXISTING CODE IN YOUR SCOPE (DO NOT recreate, EDIT if necessary)\n"
                        + "\n\n".join(content_parts))

        # 4. Read context: read-only files
        read_paths = self._resolve_read_context(cwd, task_data)
        if read_paths:
            ro_parts = []
            total_chars = 0
            max_chars = 4000
            for rel_path, abs_path in read_paths:
                try:
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(max_chars - total_chars)
                    if content.strip():
                        ro_parts.append(f"### {rel_path}\n```python\n{content}\n```")
                        total_chars += len(content)
                        if total_chars >= max_chars:
                            break
                except Exception:
                    pass
            if ro_parts:
                sections.append(
                    "## READ-ONLY REFERENCE (use as context, DO NOT MODIFY these files)\n"
                    + "\n\n".join(ro_parts))

        return "\n\n".join(sections)

    def _resolve_read_context(self, cwd: str, task_data: dict) -> list[tuple[str, str]]:
        archivos_glob = task_data.get("archivos_glob", "")
        read_context = task_data.get("read_context", "")
        paths_to_read: list[str] = []

        if read_context:
            paths_to_read.extend(p.strip() for p in read_context.split(",") if p.strip())

        if archivos_glob:
            if any(k in archivos_glob for k in ("alembic",)):
                paths_to_read.append("app/models/*.py")
            if any(k in archivos_glob for k in ("Dockerfile", "docker-compose")):
                paths_to_read.extend(["requirements.txt", "app/main.py", "pyproject.toml"])
            if any(k in archivos_glob for k in ("test", "tests")):
                paths_to_read.extend(["app/services/*.py", "app/routers/*.py"])
            if "routers" in archivos_glob:
                paths_to_read.extend(["app/schemas/*.py", "app/services/*.py"])

        resolved = []
        seen = set()
        for p in paths_to_read:
            full_path = os.path.join(cwd, p)
            if "*" in p:
                parent = os.path.dirname(full_path)
                if os.path.isdir(parent):
                    try:
                        for fname in sorted(os.listdir(parent)):
                            if fname.startswith("__"):
                                continue
                            fpath = os.path.join(parent, fname)
                            rel = os.path.join(os.path.dirname(p), fname)
                            if os.path.isfile(fpath) and rel not in seen:
                                resolved.append((rel, fpath))
                                seen.add(rel)
                    except Exception:
                        pass
            elif os.path.isfile(full_path) and p not in seen:
                resolved.append((p, full_path))
                seen.add(p)

        return resolved[:8]

    async def _handle_worker_blocked(self, ws: WorkerState, parsed: dict):
        codigo = ws.tarea_codigo
        blocker = parsed.get("blocker", "unknown")
        log.warning("Worker %s blocked: %s", codigo, blocker)
        already_recorded = any(
            a.tarea_codigo == codigo and a.failure_type == FailureType.TURN_EXHAUSTION_NO_DIFF
            for a in self._round_attempts
        )
        if not already_recorded:
            self._round_attempts.append(TaskAttempt(
                tarea_codigo=codigo,
                failure_type=FailureType.BLOCKED_DEPENDENCY,
                detail=blocker,
            ))
            self.postmortem.record(self._round_attempts[-1])
        await self.console.emit_event(
            f"Worker blocked on {codigo}: {blocker}",
            tarea_codigo=codigo, sesion_id=self.session_id,
        )
        try:
            await self.console.release_lock(ws.lock_id, self.session_id)
        except Exception:
            pass
        try:
            await self.console.patch_task(codigo, estado="blocked")
        except Exception:
            pass
        self.tasks_failed += 1

    async def _handle_worker_failure(self, ws: WorkerState, error: str):
        codigo = ws.tarea_codigo
        log.error("Worker %s failed: %s", codigo, error[:200])
        self._round_attempts.append(TaskAttempt(
            tarea_codigo=codigo,
            failure_type=FailureType.WORKER_CRASH,
            detail=error[:200],
        ))
        self.postmortem.record(self._round_attempts[-1])
        await self.console.emit_event(
            f"Worker failed on {codigo}: {error[:200]}",
            tarea_codigo=codigo, sesion_id=self.session_id,
        )
        try:
            await self.console.release_lock(ws.lock_id, self.session_id)
        except Exception:
            pass
        try:
            await self.console.patch_task(codigo, estado="review")
            log.info("Task %s moved to review (orphan, will be re-assigned)", codigo)
        except ConsoleAPIError:
            pass
        self.tasks_failed += 1

    async def _release_and_complete(self, ws: WorkerState):
        codigo = ws.tarea_codigo
        try:
            task = await self.console.get_task(codigo)
            estado = task.get("estado", "")
            if estado == "in_progress":
                await self.console.patch_task(codigo, estado="review")
                estado = "review"
            if estado in ("review", "available", "locked"):
                await self.console.patch_task(codigo, estado="done")
        except ConsoleAPIError as e:
            log.warning("Transition to done failed for %s: %s", codigo, e)
            try:
                await self.console.patch_task(codigo, estado="done")
            except Exception:
                log.error("Could not complete %s", codigo)
        try:
            await self.console.release_lock(ws.lock_id, self.session_id)
        except Exception:
            pass

        self.tasks_completed += 1
        log.info("Task %s completed. Total: %d done, %d failed, $%.4f spent",
                 codigo, self.tasks_completed, self.tasks_failed, self.total_spent)

    async def _release_and_return(self, ws: WorkerState):
        codigo = ws.tarea_codigo
        try:
            await self.console.release_lock(ws.lock_id, self.session_id)
        except Exception:
            pass
        try:
            task = await self.console.get_task(codigo)
            estado = task.get("estado", "")
            if estado == "in_progress":
                await self.console.patch_task(codigo, estado="review")
            log.info("Task %s left in review without lock (will be re-assigned)", codigo)
        except ConsoleAPIError as e:
            log.warning("Error transitioning %s after rejection: %s", codigo, e)
        except Exception:
            pass

        if ws.cwd and ws.branch_name:
            try:
                frente = await self.console.get_frente(ws.task_data.get("frente_slug", ""))
                base = frente.get("branch", "main")
                await self._git(ws.cwd, "checkout", base)
                await self._git(ws.cwd, "branch", "-D", ws.branch_name)
                log.info("Branch %s deleted after rejection", ws.branch_name)
            except Exception as e:
                log.warning("Error cleaning branch %s: %s", ws.branch_name, e)

        self.tasks_failed += 1

    # == GIT HELPERS ==

    async def _git(self, cwd: str, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {stderr.decode().strip()[:200]}")
        return stdout.decode().strip()

    # == REPO MANAGEMENT ==

    async def _ensure_repo(self, frente: dict) -> str | None:
        repo_path = self.cfg.resolve_path(
            frente.get("repo_path") or frente.get("worktree_path")
        )
        if repo_path:
            from pathlib import Path
            if Path(repo_path).joinpath(".git").exists():
                try:
                    branch = frente.get("branch", "main")
                    proc = await asyncio.create_subprocess_exec(
                        "git", "pull", "--ff-only", "origin", branch,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=repo_path,
                    )
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                    if proc.returncode == 0:
                        log.info("Repo %s updated: %s", frente["slug"],
                                 stdout.decode().strip()[:100])
                    else:
                        log.warning("git pull failed in %s: %s", repo_path,
                                    stderr.decode().strip()[:200])
                except Exception as e:
                    log.warning("Error updating repo %s: %s", repo_path, e)
                return repo_path

        if not self.cfg.workspace_dir:
            return repo_path

        from pathlib import Path
        workspace = Path(self.cfg.workspace_dir)
        workspace.mkdir(parents=True, exist_ok=True)

        slug = frente["slug"]
        local_path = workspace / slug

        if local_path.joinpath(".git").exists():
            try:
                branch = frente.get("branch", "main")
                proc = await asyncio.create_subprocess_exec(
                    "git", "pull", "--ff-only", "origin", branch,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(local_path),
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                if proc.returncode == 0:
                    log.info("Workspace repo %s updated", slug)
                else:
                    log.warning("git pull failed in workspace %s: %s", slug,
                                stderr.decode().strip()[:200])
            except Exception as e:
                log.warning("Error pull workspace %s: %s", slug, e)
            return str(local_path)

        repo_name = self.cfg.repo_map.get(slug, slug)
        log.info("Cloning %s/%s -> %s", self.cfg.github_org, repo_name, local_path)
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "repo", "clone", f"{self.cfg.github_org}/{repo_name}",
                str(local_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0:
                log.info("Repo %s cloned to %s", slug, local_path)
                return str(local_path)
            else:
                err = stderr.decode().strip()
                log.error("Clone failed for %s: %s", slug, err[:300])
                return None
        except Exception as e:
            log.error("Error cloning %s: %s", slug, e)
            return None

    # == PLANNER ==

    async def plan_project(self, brief: str):
        log.info("=== PLANNER: decomposing brief (%d chars) ===", len(brief))

        try:
            plan_snapshot = await self.console.get_plan_snapshot()
        except Exception:
            plan_snapshot = ""

        existing_knowledge = self.shared_ctx.format_for_prompt()

        system, user = build_planner_prompt(
            brief=brief,
            system_context=plan_snapshot,
            existing_knowledge=existing_knowledge,
            frentes=self.cfg.frentes,
        )

        result = await self.runner.run(
            prompt=user,
            model="opus",
            max_turns=3,
            max_budget_usd=self.cfg.architect_budget_usd,
            system_prompt=system,
            allowed_tools=[],
        )
        self.total_spent += result.cost_usd

        if not result.success:
            log.error("Planner failed: %s", result.result_text[:300])
            return None

        try:
            text = result.result_text
            match = re.search(r'\{[\s\S]*"tareas"[\s\S]*\}', text)
            if match:
                plan = json.loads(match.group(0))
            else:
                plan = json.loads(text)
        except json.JSONDecodeError:
            log.error("Planner returned invalid JSON: %s", result.result_text[:300])
            return None

        tareas = plan.get("tareas", [])
        frente = plan.get("frente", self.cfg.frentes[0] if self.cfg.frentes else "default")
        int_branch = plan.get("integration_branch", "")

        if int_branch and not self.cfg.integration_branch:
            self.cfg.integration_branch = int_branch
            log.info("Integration branch set: %s", int_branch)

        created = 0
        research_tasks = []
        for t in tareas:
            codigo = t.get("codigo", "")
            if not codigo:
                continue
            try:
                await self.console.create_task(
                    frente=frente,
                    codigo=codigo,
                    titulo=t.get("titulo", ""),
                    descripcion=t.get("descripcion"),
                    prioridad=t.get("prioridad", 2),
                    blocker_tarea_codigo=t.get("blocker_tarea_codigo"),
                    estimacion_min=t.get("estimacion_min"),
                    modelo_sugerido=t.get("modelo_sugerido"),
                    archivos_glob=t.get("archivos_glob"),
                )
                created += 1
                if t.get("requiere_investigacion"):
                    research_tasks.append(t)
                log.info("Task created: %s — %s", codigo, t.get("titulo", ""))
            except ConsoleAPIError as e:
                if e.status == 409:
                    log.debug("Task %s already exists, skipping", codigo)
                else:
                    log.warning("Error creating task %s: %s", codigo, e)

        log.info("Planner created %d/%d tasks, %d require research",
                 created, len(tareas), len(research_tasks))

        await self.console.emit_event(
            f"Plan generated: {created} tasks, {len(research_tasks)} research. "
            f"Front: {frente}. Reason: {plan.get('reasoning', '')}",
            frente=frente, sesion_id=self.session_id,
            payload=plan,
        )

        return {"plan": plan, "research_tasks": research_tasks, "frente": frente}

    # == RESEARCHER ==

    async def _run_research_phase(self, research_tasks: list[dict]):
        if not research_tasks:
            log.info("No pending research tasks")
            return

        log.info("=== RESEARCH: %d tasks ===", len(research_tasks))

        for task in research_tasks:
            codigo = task.get("codigo", "?")

            if self.total_spent >= self.cfg.max_budget_usd * 0.5:
                log.warning("Budget at 50%% ($%.2f), pausing research", self.total_spent)
                break

            conocimiento = self.shared_ctx.format_for_prompt()

            preguntas = task.get("preguntas_investigacion", [])
            if not preguntas:
                preguntas = [task.get("titulo", "Research")]

            system, user = build_researcher_prompt(
                task=task,
                preguntas=preguntas,
                conocimiento_previo=conocimiento,
                contexto=task.get("descripcion", ""),
            )

            log.info("Researcher launched: %s — %s", codigo, task.get("titulo", ""))

            result = await self.runner.run(
                prompt=user,
                model="sonnet",
                max_turns=15,
                max_budget_usd=self.cfg.worker_budget_usd,
                system_prompt=system,
                allowed_tools=RESEARCHER_TOOLS,
            )
            self.total_spent += result.cost_usd

            if not result.success:
                log.warning("Researcher %s failed: %s", codigo, result.result_text[:200])
                continue

            parsed = self._parse_worker_result(result.result_text)
            findings = parsed.get("findings", [])

            if findings:
                for f in findings:
                    f["tarea_codigo"] = codigo
                    f.setdefault("tipo", "research_finding")
                    if f.get("fuente"):
                        f["contenido"] = f"{f.get('contenido', '')} [source: {f['fuente']}]"
                count = self.shared_ctx.add_many(findings)
                log.info("Researcher %s: %d findings saved", codigo, count)
            else:
                log.info("Researcher %s: no structured findings", codigo)
                if parsed.get("summary"):
                    self.shared_ctx.add_decision(
                        codigo, "research_finding", parsed["summary"],
                        tags=["auto-summary"],
                    )

            try:
                await self.console.patch_task(codigo, estado="done")
            except Exception:
                pass

            await self.console.emit_event(
                f"Research {codigo} completed: {len(findings)} findings. "
                f"${result.cost_usd:.4f}, {result.duration_s:.0f}s",
                tarea_codigo=codigo, sesion_id=self.session_id,
            )

    # == ARCHITECT ==

    async def _find_orphan_tasks(self, frente: str) -> list[dict]:
        orphans = []
        try:
            locks = await self.console.list_locks()
            locked_codigos = {l.get("tarea_codigo") for l in locks}

            for estado in ("review", "in_progress"):
                tasks = await self.console.get_tasks_by_estado(frente, estado)
                for t in tasks:
                    codigo = t.get("codigo", "")
                    if codigo not in locked_codigos and codigo not in self.active_workers:
                        max_allowed = self.cfg.max_task_retries + 1
                        if self.shared_ctx.count_rejections(codigo) >= max_allowed:
                            log.debug("Task %s excluded: %d+ rejections (requires human intervention)",
                                      codigo, max_allowed)
                            continue
                        if t.get("estado") == "review":
                            try:
                                await self.console.patch_task(codigo, estado="in_progress")
                            except ConsoleAPIError:
                                continue
                        orphans.append(t)
                        log.info("Orphan task detected: %s (was %s, no lock)", codigo, estado)
        except Exception as e:
            log.debug("Error finding orphans in %s: %s", frente, e)
        return orphans

    async def _architect_cycle(self, available_slots: int):
        disponibles: dict[str, list] = {}
        for frente in self.cfg.frentes:
            try:
                tasks = await self.console.get_available_tasks(frente, limit=5)
                orphans = await self._find_orphan_tasks(frente)
                if orphans:
                    log.info("Re-injecting %d orphan tasks in %s", len(orphans), frente)
                tasks.extend(orphans)
                disponibles[frente] = tasks
            except Exception as e:
                log.warning("Error getting available tasks from %s: %s", frente, e)
                disponibles[frente] = []

        if not any(disponibles.values()):
            log.debug("No available tasks in any front")
            return

        try:
            plan_snapshot = await self.console.get_plan_snapshot()
            stats = await self.console.get_stats()
            skills = await self.console.list_skills()
            locks = await self.console.list_locks()
        except Exception as e:
            log.warning("Error getting context: %s", e)
            return

        # Fetch recent chat messages from the architect chat
        chat_context = ""
        try:
            chat_messages = await self.console.get_architect_messages(limit=20)
            if chat_messages:
                lines = []
                for msg in chat_messages:
                    lines.append(f"[{msg['role'].upper()}] {msg['content']}")
                chat_context = "\n".join(lines)
        except Exception as e:
            log.debug("Could not fetch architect chat: %s", e)

        system, user = build_architect_prompt(
            plan_snapshot, stats, disponibles, locks, skills, available_slots,
            chat_context=chat_context,
        )

        if self.cfg.dry_run:
            log.info("[DRY RUN] Architect consulted with %d tasks available in %d fronts",
                     sum(len(t) for t in disponibles.values()), len(self.cfg.frentes))
            log.info("[DRY RUN] Architect prompt: %d chars", len(system) + len(user))
            return

        result = await self.runner.run(
            prompt=user,
            model=self.cfg.architect_model,
            max_turns=1,
            max_budget_usd=self.cfg.architect_budget_usd,
            system_prompt=system,
            allowed_tools=[],
        )
        self.total_spent += result.cost_usd

        if not result.success:
            log.error("Architect failed: %s", result.result_text[:200])
            return

        try:
            text = result.result_text
            match = re.search(r'\{[\s\S]*"assignments"[\s\S]*\}', text)
            if match:
                decision = json.loads(match.group(0))
            else:
                decision = json.loads(text)
        except json.JSONDecodeError:
            log.error("Architect returned invalid JSON: %s", result.result_text[:300])
            return

        if decision.get("skip_cycle"):
            log.info("Architect says skip: %s", decision.get("reasoning", ""))
            return

        assignments = decision.get("assignments", [])
        log.info("Architect assigns %d tasks: %s — %s",
                 len(assignments),
                 [a["tarea_codigo"] for a in assignments],
                 decision.get("reasoning", ""))

        await self.console.emit_event(
            f"Architect assigns: {[a['tarea_codigo'] for a in assignments]}. "
            f"Reason: {decision.get('reasoning', '')}",
            frente=self.cfg.frentes[0],
            sesion_id=self.session_id,
            payload=decision,
        )

        # Post architect response and assignments to chat
        try:
            arch_msg = await self.console.post_architect_message(
                content=result.result_text,
                role="architect",
                session_id=str(self.session_id),
                metadata={"model": self.cfg.architect_model, "cost_usd": result.cost_usd},
            )
            if assignments:
                await self.console.post_architect_assignments(arch_msg["id"], assignments)
                if not self.cfg.architect_auto_approve:
                    log.info("Auto-approve OFF — waiting for manual approval of %d assignments",
                             len(assignments))
                    return
        except Exception as e:
            log.debug("Could not post architect message to chat: %s", e)

        for assignment in assignments[:available_slots]:
            remaining = self.cfg.max_budget_usd - self.total_spent
            if remaining < self.cfg.worker_budget_usd * 0.5:
                log.warning("Insufficient budget for more workers ($%.2f remaining)", remaining)
                break
            await self._spawn_worker(assignment)

    async def _spawn_worker(self, assignment: dict):
        codigo = assignment["tarea_codigo"]
        model = assignment.get("model", self.cfg.worker_model)
        skill_slugs = assignment.get("skills", [])
        hint = assignment.get("worker_hint", "")

        try:
            lock = await self.console.acquire_lock(codigo, self.session_id, self.cfg.lock_ttl_min)
        except ConsoleAPIError as e:
            log.warning("Could not acquire lock for %s: %s", codigo, e.detail)
            return

        lock_id = lock["id"]

        try:
            await self.console.patch_task(codigo, estado="in_progress")
        except Exception:
            pass

        task_data = await self.console.get_task(codigo)

        skills_md = ""
        for slug in skill_slugs:
            try:
                content = await self.console.get_skill_content(slug)
                skills_md += f"\n---\n## SKILL: {slug}\n{content}\n"
            except Exception:
                log.debug("Skill '%s' not available", slug)

        frente_slug = task_data.get("frente_slug", "")
        shared_decisions = self.shared_ctx.format_for_prompt(frente=frente_slug)

        prior_rejections = self.shared_ctx.format_rejections(codigo)
        if prior_rejections:
            rejection_count = self.shared_ctx.count_rejections(codigo)
            shared_decisions += (
                f"\n\n## PRIOR REJECTIONS FOR THIS TASK ({rejection_count} failed attempts)\n"
                f"DO NOT repeat these errors. The reviewer will reject you again if you make them:\n"
                f"{prior_rejections}"
            )
            log.info("Worker %s receives %d prior rejections as feedback", codigo, rejection_count)

        is_infra = self._is_infra_task(task_data, hint)

        if is_infra:
            prompt = build_infra_worker_prompt(task_data, hint)
            log.info("Worker %s detected as INFRA", codigo)
        else:
            prompt = build_worker_prompt(
                task_data, hint, skills_md,
                shared_decisions=shared_decisions,
                docker_target=self.cfg.docker_target,
            )

        frente = await self.console.get_frente(task_data.get("frente_slug", ""))
        pf = await self.preflight.check(
            frente=frente,
            integration_branch=self.cfg.integration_branch,
            task_data=task_data,
        )

        if not pf.ok:
            log.error("Preflight failed for %s: %s — aborting spawn", codigo, pf.error)
            await self.console.emit_event(
                f"Preflight FAIL {codigo}: {pf.error}",
                tarea_codigo=codigo, sesion_id=self.session_id,
            )
            try:
                await self.console.release_lock(lock_id, self.session_id)
            except Exception:
                pass
            return

        cwd = pf.cwd
        if pf.repairs:
            await self.console.emit_event(
                f"Preflight {codigo}: {len(pf.repairs)} repairs — {'; '.join(pf.repairs[:3])}",
                tarea_codigo=codigo, sesion_id=self.session_id,
            )

        branch_name = f"orch/{codigo.lower()}"
        if cwd:
            try:
                await self._git(cwd, "checkout", "--", ".")
                proc = await asyncio.create_subprocess_exec(
                    "git", "clean", "-fd",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
                await proc.communicate()

                base_branch = pf.base_branch
                if self.cfg.integration_branch:
                    try:
                        await self._git(cwd, "rev-parse", "--verify", self.cfg.integration_branch)
                        base_branch = self.cfg.integration_branch
                        log.info("Using integration branch %s as base for %s", base_branch, codigo)
                    except RuntimeError:
                        pass

                await self._git(cwd, "checkout", base_branch)
                if base_branch != self.cfg.integration_branch:
                    try:
                        await self._git(cwd, "pull", "--ff-only", "origin", base_branch)
                    except RuntimeError:
                        log.debug("Pull failed (possibly local repo without remote), continuing")
                await self._git(cwd, "checkout", "-B", branch_name)
                log.info("Branch %s created from clean %s for %s", branch_name, base_branch, codigo)
            except Exception as e:
                log.warning("Error creating branch %s: %s", branch_name, e)

        head_before = ""
        if cwd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", "rev-parse", "HEAD",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
                stdout, _ = await proc.communicate()
                head_before = stdout.decode().strip()
            except Exception:
                pass

        if cwd:
            repo_context = await self._preprocess_repo_context(cwd, task_data)
            if repo_context:
                prompt += f"\n\n{repo_context}"

        await self.console.emit_event(
            f"Worker launched for {codigo} (model={model}, cwd={cwd})",
            tarea_codigo=codigo, sesion_id=self.session_id,
        )

        if is_infra:
            tools = INFRA_TOOLS
        elif self.cfg.docker_target:
            tools = WORKER_TOOLS_DOCKER
        else:
            tools = WORKER_TOOLS

        async def _run():
            return await self.runner.run(
                prompt=prompt,
                cwd=cwd,
                model=model,
                max_turns=self.cfg.worker_max_turns,
                max_budget_usd=min(
                    self.cfg.worker_budget_usd,
                    self.cfg.max_budget_usd - self.total_spent,
                ),
                allowed_tools=tools,
            )

        atask = asyncio.create_task(_run())
        self.active_workers[codigo] = WorkerState(
            tarea_codigo=codigo,
            task_data=task_data,
            lock_id=lock_id,
            asyncio_task=atask,
            skills_content=skills_md,
            started_at=time.monotonic(),
            head_before=head_before,
            branch_name=branch_name,
            cwd=cwd or "",
        )
        log.info("Worker spawned: %s (model=%s, lock=%d)", codigo, model, lock_id)

    # == INFRA DETECTION + VALIDATION ==

    def _is_infra_task(self, task_data: dict, hint: str) -> bool:
        keywords = ("scaffold", "setup", "init", "infra", "bootstrap", "docker-compose",
                    "alembic init", "base structure", "initial configuration")
        combined = f"{task_data.get('titulo', '')} {task_data.get('descripcion', '')} {hint}".lower()

        if any(k in combined for k in keywords):
            return True

        archivos = (task_data.get("archivos_glob") or "").lower()
        infra_files = ("dockerfile", "docker-compose", "alembic.ini", "pyproject.toml",
                       "cargo.toml", ".env", "requirements.txt")
        infra_count = sum(1 for f in infra_files if f in archivos)
        if infra_count >= 2:
            return True

        return False

    async def _run_validation_gate(self, cwd: str, frente_slug: str) -> dict | None:
        if not cwd:
            return None

        stack_info = "Unknown"
        if os.path.exists(os.path.join(cwd, "requirements.txt")):
            stack_info = "Python/FastAPI"
        elif os.path.exists(os.path.join(cwd, "Cargo.toml")):
            stack_info = "Rust/Axum"

        _, prompt = build_validation_prompt(cwd, stack_info)

        log.info("Validation gate launched for %s (stack=%s)", frente_slug, stack_info)

        result = await self.runner.run(
            prompt=prompt,
            cwd=cwd,
            model="sonnet",
            max_turns=5,
            max_budget_usd=0.5,
            allowed_tools=INFRA_TOOLS,
        )
        self.total_spent += result.cost_usd

        if not result.success:
            log.warning("Validation gate failed: %s", result.result_text[:200])
            return None

        try:
            text = result.result_text
            match = re.search(r'\{[\s\S]*"status"[\s\S]*"checks"[\s\S]*\}', text)
            if match:
                parsed = json.loads(match.group(0))
            else:
                parsed = json.loads(text)
        except json.JSONDecodeError:
            log.warning("Validation gate returned invalid JSON")
            return None

        status = parsed.get("status", "fail")
        summary = parsed.get("summary", "")
        log.info("Validation gate: %s — %s", status, summary)

        await self.console.emit_event(
            f"Validation gate: {status} — {summary}",
            frente=frente_slug, sesion_id=self.session_id,
            payload=parsed,
        )

        return parsed

    # == MERGE PIPELINE ==

    async def _merge_to_integration(self, ws: WorkerState):
        if not self.cfg.integration_branch or not ws.cwd or not ws.branch_name:
            return

        int_branch = self.cfg.integration_branch
        codigo = ws.tarea_codigo

        try:
            try:
                await self._git(ws.cwd, "rev-parse", "--verify", int_branch)
            except RuntimeError:
                frente = await self.console.get_frente(ws.task_data.get("frente_slug", ""))
                base = frente.get("branch", "main")
                await self._git(ws.cwd, "branch", int_branch, base)
                log.info("Integration branch %s created from %s", int_branch, base)

            await self._git(ws.cwd, "checkout", int_branch)
            await self._git(ws.cwd, "merge", "--no-ff", ws.branch_name,
                            "-m", f"Merge {codigo}: {ws.task_data.get('titulo', '')}")
            log.info("Branch %s merged to %s", ws.branch_name, int_branch)

            await self.console.emit_event(
                f"{codigo} merged to {int_branch}",
                tarea_codigo=codigo, sesion_id=self.session_id,
            )

        except RuntimeError as e:
            err = str(e)
            if "CONFLICT" in err or "conflict" in err.lower():
                log.error("Merge conflict merging %s to %s: %s", ws.branch_name, int_branch, err[:200])
                try:
                    await self._git(ws.cwd, "merge", "--abort")
                except Exception:
                    pass
                await self.console.emit_event(
                    f"CONFLICT: {codigo} could not be merged to {int_branch}. Requires manual resolution.",
                    tarea_codigo=codigo, sesion_id=self.session_id,
                )
            else:
                log.warning("Error merge %s to %s: %s", ws.branch_name, int_branch, err[:200])

    # == REVIEWER ==

    async def _run_reviewer(self, ws: WorkerState, git_diff: str, worker_summary: str,
                            shared_decisions: str = "") -> ClaudeResult:
        system, user = build_reviewer_prompt(
            ws.task_data, git_diff, worker_summary, ws.skills_content,
            shared_decisions=shared_decisions,
        )
        return await self.runner.run(
            prompt=user,
            model=self.cfg.reviewer_model,
            max_turns=2,
            max_budget_usd=self.cfg.reviewer_budget_usd,
            system_prompt=system,
            allowed_tools=[],
        )

    def _parse_reviewer_result(self, text: str) -> dict:
        match = re.search(r'\{[\s\S]*"verdict"[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {"verdict": "approve", "issues": [], "summary": "Auto-approve (reviewer did not return JSON)"}
