"""Shared memory between workers — append-only, immutable."""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("orchestrator.context")


@dataclass
class Decision:
    id: int
    tarea_codigo: str
    tipo: str
    contenido: str
    frente: str
    timestamp: float
    tags: list[str]

    VALID_TYPES = {
        "schema_decision", "api_contract", "naming_convention",
        "business_rule", "dependency", "note",
        "research_finding", "library_evaluation", "license_check",
        "architecture_option", "reference_doc", "legal_constraint",
        "state_of_art",
        "review_rejection",
        "file_inventory",
    }

    def to_prompt_line(self) -> str:
        tags_str = f" [{', '.join(self.tags)}]" if self.tags else ""
        return f"- **[{self.tipo}]** ({self.tarea_codigo}{tags_str}): {self.contenido}"


class SharedContext:
    """SQLite append-only store for decisions between workers."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._ensure_db()

    def _ensure_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tarea_codigo TEXT NOT NULL,
                    tipo TEXT NOT NULL,
                    contenido TEXT NOT NULL,
                    frente TEXT DEFAULT '',
                    timestamp REAL NOT NULL,
                    tags TEXT DEFAULT '[]'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_decisions_frente
                ON decisions(frente)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_decisions_tipo
                ON decisions(tipo)
            """)
        log.info("SharedContext DB: %s", self.db_path)

    def add_decision(
        self,
        tarea_codigo: str,
        tipo: str,
        contenido: str,
        frente: str = "",
        tags: list[str] | None = None,
    ) -> int:
        tags_json = json.dumps(tags or [])
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO decisions (tarea_codigo, tipo, contenido, frente, timestamp, tags) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tarea_codigo, tipo, contenido, frente, time.time(), tags_json),
            )
            did = cur.lastrowid
        log.debug("Decision #%d saved: [%s] %s — %s", did, tipo, tarea_codigo, contenido[:80])
        return did

    def add_many(self, decisions: list[dict]) -> int:
        count = 0
        for d in decisions:
            if not d.get("contenido"):
                continue
            self.add_decision(
                tarea_codigo=d.get("tarea_codigo", "?"),
                tipo=d.get("tipo", "note"),
                contenido=d["contenido"],
                frente=d.get("frente", ""),
                tags=d.get("tags"),
            )
            count += 1
        return count

    def get_decisions(
        self,
        frente: str | None = None,
        tipo: str | None = None,
        limit: int = 50,
    ) -> list[Decision]:
        query = "SELECT id, tarea_codigo, tipo, contenido, frente, timestamp, tags FROM decisions"
        conditions = []
        params: list = []

        if frente:
            conditions.append("frente = ?")
            params.append(frente)
        if tipo:
            conditions.append("tipo = ?")
            params.append(tipo)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()

        return [
            Decision(
                id=r[0], tarea_codigo=r[1], tipo=r[2], contenido=r[3],
                frente=r[4], timestamp=r[5], tags=json.loads(r[6] or "[]"),
            )
            for r in reversed(rows)
        ]

    def get_recent(self, n: int = 20) -> list[Decision]:
        return self.get_decisions(limit=n)

    def format_for_prompt(
        self,
        frente: str | None = None,
        limit: int = 30,
    ) -> str:
        decisions = self.get_decisions(frente=frente, limit=limit)
        if not decisions:
            return "(no prior decisions)"
        lines = [d.to_prompt_line() for d in decisions]
        return "\n".join(lines)

    def get_rejections(self, tarea_codigo: str) -> list[Decision]:
        return self.get_decisions(tipo="review_rejection")

    def count_rejections(self, tarea_codigo: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE tarea_codigo = ? AND tipo = 'review_rejection'",
                (tarea_codigo,),
            ).fetchone()[0]

    def format_rejections(self, tarea_codigo: str) -> str:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT contenido FROM decisions WHERE tarea_codigo = ? AND tipo = 'review_rejection' ORDER BY id",
                (tarea_codigo,),
            ).fetchall()
        if not rows:
            return ""
        lines = [f"ATTEMPT {i+1} REJECTED: {r[0]}" for i, r in enumerate(rows)]
        return "\n".join(lines)

    def get_failure_history(self) -> dict[str, list[dict]]:
        history: dict[str, list[dict]] = {}
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT tarea_codigo, contenido, tags FROM decisions "
                "WHERE tipo = 'review_rejection' ORDER BY id"
            ).fetchall()
            for codigo, contenido, tags_json in rows:
                history.setdefault(codigo, [])
                if "SCOPE VIOLATION" in contenido:
                    history[codigo].append({"type": "scope_violation", "detail": contenido})
                elif "AUTO-CONSTRAINT" in contenido:
                    continue
                else:
                    history[codigo].append({"type": "reviewer_rejection", "detail": contenido})

            done_rows = conn.execute(
                "SELECT DISTINCT tarea_codigo FROM decisions WHERE tipo = 'file_inventory'"
            ).fetchall()
            for (codigo,) in done_rows:
                history.setdefault(codigo, [])
                history[codigo].append({"type": "completed"})

        return history

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
