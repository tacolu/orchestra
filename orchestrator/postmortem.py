"""PostMortem analyzer — auto-correction between rounds.

Classifies failures from each round and generates automatic adjustments:
- Expand scope after repeated scope violations
- Add read_context after turn exhaustion without diff
- Inject constraints after repeated reviewer rejections for same reason
- Request split after 3+ failed attempts on same task
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("orchestrator.postmortem")


class FailureType(str, Enum):
    SCOPE_VIOLATION = "scope_violation"
    TURN_EXHAUSTION_NO_DIFF = "turn_exhaustion_no_diff"
    TURN_EXHAUSTION_WITH_DIFF = "turn_exhaustion_with_diff"
    REVIEWER_REJECTION = "reviewer_rejection"
    BLOCKED_DEPENDENCY = "blocked_dependency"
    WORKER_CRASH = "worker_crash"
    COMPLETED = "completed"


class AdjustmentType(str, Enum):
    EXPAND_SCOPE = "expand_scope"
    ADD_READ_CONTEXT = "add_read_context"
    INJECT_CONSTRAINT = "inject_constraint"
    REQUEST_SPLIT = "request_split"
    MARK_NEEDS_HUMAN = "mark_needs_human"


@dataclass
class TaskAttempt:
    tarea_codigo: str
    failure_type: FailureType
    detail: str = ""
    out_of_scope_files: list[str] = field(default_factory=list)
    rejection_issues: list[str] = field(default_factory=list)
    turns_used: int = 0
    cost_usd: float = 0.0


@dataclass
class Adjustment:
    tipo: AdjustmentType
    tarea_codigo: str
    description: str
    new_scope: str = ""
    read_context: str = ""
    constraint: str = ""
    auto_apply: bool = True


class PostMortemAnalyzer:
    """Analyzes results from a round and proposes automatic adjustments."""

    def __init__(self):
        self.history: dict[str, list[TaskAttempt]] = {}
        self.round_number: int = 0

    def hydrate(self, failure_history: dict[str, list[dict]]):
        type_map = {
            "scope_violation": FailureType.SCOPE_VIOLATION,
            "reviewer_rejection": FailureType.REVIEWER_REJECTION,
            "completed": FailureType.COMPLETED,
        }
        total = 0
        for codigo, entries in failure_history.items():
            for entry in entries:
                ft = type_map.get(entry["type"])
                if not ft:
                    continue
                attempt = TaskAttempt(
                    tarea_codigo=codigo,
                    failure_type=ft,
                    detail=entry.get("detail", ""),
                )
                if ft == FailureType.SCOPE_VIOLATION and "out of scope:" in entry.get("detail", ""):
                    files_match = re.search(r"out of scope: (.+?)\.", entry["detail"])
                    if files_match:
                        attempt.out_of_scope_files = [f.strip() for f in files_match.group(1).split(",")]
                if ft == FailureType.REVIEWER_REJECTION:
                    attempt.rejection_issues = [entry.get("detail", "")]
                self.history.setdefault(codigo, []).append(attempt)
                total += 1
        if total:
            log.info("PostMortem hydrated: %d historical attempts from %d tasks", total, len(failure_history))

    def record(self, attempt: TaskAttempt):
        if attempt.tarea_codigo not in self.history:
            self.history[attempt.tarea_codigo] = []
        self.history[attempt.tarea_codigo].append(attempt)

    def analyze_round(self, round_attempts: list[TaskAttempt]) -> list[Adjustment]:
        self.round_number += 1
        adjustments: list[Adjustment] = []

        by_task: dict[str, list[TaskAttempt]] = {}
        for a in round_attempts:
            by_task.setdefault(a.tarea_codigo, []).append(a)

        for codigo, attempts in by_task.items():
            all_attempts = self.history.get(codigo, [])
            adjustments.extend(self._analyze_task(codigo, attempts, all_attempts))

        if adjustments:
            log.info("PostMortem round %d: %d adjustments proposed", self.round_number, len(adjustments))
            for adj in adjustments:
                log.info("  [%s] %s: %s", adj.tipo.value, adj.tarea_codigo, adj.description)
        else:
            log.info("PostMortem round %d: no adjustments needed", self.round_number)

        return adjustments

    def _analyze_task(
        self, codigo: str,
        current: list[TaskAttempt],
        all_history: list[TaskAttempt],
    ) -> list[Adjustment]:
        adjustments = []

        def count_type(ft: FailureType) -> int:
            return sum(1 for a in all_history if a.failure_type == ft)

        # R1: Repeated scope violations -> expand scope
        scope_violations = [a for a in all_history if a.failure_type == FailureType.SCOPE_VIOLATION]
        if len(scope_violations) >= 2:
            all_oos = set()
            for sv in scope_violations:
                all_oos.update(sv.out_of_scope_files)
            if all_oos:
                new_dirs = set()
                for f in all_oos:
                    parent = "/".join(f.split("/")[:-1]) if "/" in f else "."
                    new_dirs.add(parent)
                adjustments.append(Adjustment(
                    tipo=AdjustmentType.EXPAND_SCOPE,
                    tarea_codigo=codigo,
                    description=f"Scope violation x{len(scope_violations)}, expand to: {', '.join(sorted(new_dirs))}",
                    new_scope=",".join(f"{d}/*" for d in sorted(new_dirs)),
                    auto_apply=True,
                ))

        # R2: Turn exhaustion without diff >=2 -> add read_context
        no_diff_count = count_type(FailureType.TURN_EXHAUSTION_NO_DIFF)
        if no_diff_count >= 2:
            adjustments.append(Adjustment(
                tipo=AdjustmentType.ADD_READ_CONTEXT,
                tarea_codigo=codigo,
                description=f"Turn exhaustion without diff x{no_diff_count}, worker in exploration loop",
                read_context="app/models/*.py,app/main.py,requirements.txt",
                auto_apply=True,
            ))

        # R3: Reviewer rejects >=2 times for same reason -> inject constraint
        rejections = [a for a in all_history if a.failure_type == FailureType.REVIEWER_REJECTION]
        if len(rejections) >= 2:
            all_issues = []
            for r in rejections:
                all_issues.extend(r.rejection_issues)
            common = self._find_common_patterns(all_issues)
            if common:
                adjustments.append(Adjustment(
                    tipo=AdjustmentType.INJECT_CONSTRAINT,
                    tarea_codigo=codigo,
                    description=f"Reviewer rejects x{len(rejections)}, common pattern: {common}",
                    constraint=f"CRITICAL CONSTRAINT (rejected {len(rejections)} times): {common}",
                    auto_apply=True,
                ))

        # R4: 3+ total attempts -> request split
        total_failures = sum(1 for a in all_history if a.failure_type != FailureType.COMPLETED)
        if total_failures >= 3:
            already_split = any(
                isinstance(a, Adjustment) and a.tipo == AdjustmentType.REQUEST_SPLIT
                for a in adjustments
            )
            if not already_split:
                adjustments.append(Adjustment(
                    tipo=AdjustmentType.REQUEST_SPLIT,
                    tarea_codigo=codigo,
                    description=f"{total_failures} failed attempts, task too complex for a single worker",
                    auto_apply=False,
                ))

        # R5: 5+ attempts -> mark as needs_human
        if total_failures >= 5:
            adjustments.append(Adjustment(
                tipo=AdjustmentType.MARK_NEEDS_HUMAN,
                tarea_codigo=codigo,
                description=f"{total_failures} failed attempts, requires human intervention",
                auto_apply=True,
            ))

        return adjustments

    def _find_common_patterns(self, issues: list[str]) -> str:
        if not issues:
            return ""

        keywords: dict[str, int] = {}
        stop_words = {"the", "a", "is", "in", "to", "and", "of", "for", "not", "with",
                      "el", "la", "de", "en", "que", "no", "un", "una", "los", "las",
                      "por", "con", "del", "al", "se", "es"}
        for issue in issues:
            words = set(re.findall(r'\b\w{3,}\b', issue.lower())) - stop_words
            for w in words:
                keywords[w] = keywords.get(w, 0) + 1

        threshold = max(2, len(issues) // 2)
        common_words = sorted(
            [w for w, c in keywords.items() if c >= threshold],
            key=lambda w: -keywords[w],
        )

        if not common_words:
            return issues[0][:100] if issues else ""

        return ", ".join(common_words[:5])

    def get_stats(self) -> dict:
        total = sum(len(v) for v in self.history.values())
        by_type: dict[str, int] = {}
        for attempts in self.history.values():
            for a in attempts:
                by_type[a.failure_type.value] = by_type.get(a.failure_type.value, 0) + 1
        return {
            "rounds": self.round_number,
            "total_attempts": total,
            "unique_tasks": len(self.history),
            "by_type": by_type,
        }
