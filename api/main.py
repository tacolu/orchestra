"""
Orchestra Console — API FastAPI

SQLite with WAL as source of truth. Raw queries for fine-grained
concurrency control.

Launch:
    uvicorn main:app --host 0.0.0.0 --port 8200 --reload
"""

import asyncio
import json as _json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

# ─── Configuration ──────────────────────────────────────────────────────────
DB_PATH = os.getenv("ORCHESTRA_DB", "/data/orchestra.db")
SKILLS_DIRS = os.getenv(
    "ORCHESTRA_SKILLS_DIRS",
    "/skills",
).split(":")

START_TS = time.time()

app = FastAPI(
    title="Orchestra Console API",
    version="1.0.0",
    description="Multi-agent AI orchestration. Task coordination, locks, events, skills.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── SQLite connection ────────────────────────────────────────────────────────
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row else None


def rows_to_list(rows) -> list[dict]:
    return [dict(r) for r in rows]


def err(status: int, code: str, detail: str):
    raise HTTPException(status_code=status, detail={"error": code, "detail": detail})


# ─── Model suggestion heuristic ──────────────────────────────────────────────
def modelo_por_estimacion(min_est: Optional[int]) -> Optional[str]:
    """
    Heuristic by time estimate when task doesn't declare a model.
      < 30 min   → small model (classification, validation)
      30-120 min → medium model (code following patterns)
      > 120 min  → large model (new design, deep debugging)
    """
    if min_est is None:
        return None
    if min_est < 30:
        return "small"
    if min_est <= 120:
        return "medium"
    return "large"


def modelo_efectivo(tarea_row: dict) -> Optional[str]:
    return tarea_row.get("modelo_sugerido") or modelo_por_estimacion(tarea_row.get("estimacion_min"))


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class FrenteIn(BaseModel):
    slug: str
    nombre: str
    descripcion: Optional[str] = None
    color: Optional[str] = None
    icono: Optional[str] = None
    repo_path: Optional[str] = None
    worktree_path: Optional[str] = None
    branch: Optional[str] = None


class FrentePatch(BaseModel):
    nombre: Optional[str] = None
    descripcion: Optional[str] = None
    color: Optional[str] = None
    icono: Optional[str] = None
    repo_path: Optional[str] = None
    worktree_path: Optional[str] = None
    branch: Optional[str] = None
    estado: Optional[str] = None


class ObjetivoIn(BaseModel):
    frente: str
    titulo: str
    descripcion: Optional[str] = None
    prioridad: int = 0
    deadline: Optional[str] = None


class ObjetivoPatch(BaseModel):
    titulo: Optional[str] = None
    descripcion: Optional[str] = None
    prioridad: Optional[int] = None
    estado: Optional[str] = None
    deadline: Optional[str] = None


class TareaIn(BaseModel):
    frente: str
    codigo: str
    titulo: str
    descripcion: Optional[str] = None
    objetivo_id: Optional[int] = None
    prioridad: int = 0
    blocker_tarea_codigo: Optional[str] = None
    archivos_glob: Optional[str] = None
    estimacion_min: Optional[int] = None
    modelo_sugerido: Optional[str] = None


class TareaPatch(BaseModel):
    titulo: Optional[str] = None
    descripcion: Optional[str] = None
    objetivo_id: Optional[int] = None
    prioridad: Optional[int] = None
    blocker_tarea_codigo: Optional[str] = None
    archivos_glob: Optional[str] = None
    estimacion_min: Optional[int] = None
    modelo_sugerido: Optional[str] = None
    estado: Optional[str] = None


class SesionIn(BaseModel):
    nombre_tmux: str
    frente: str
    host: str
    pid: Optional[int] = None
    pwd: Optional[str] = None
    metadatos: Optional[dict] = None


class CierreSesion(BaseModel):
    motivo: Optional[str] = None
    nota_diario: Optional[str] = None


class LockIn(BaseModel):
    tarea_codigo: str
    sesion_id: int
    ttl_min: int = Field(default=30, ge=1, le=120)
    notas: Optional[str] = None


class EventoIn(BaseModel):
    tipo: str = "note"
    sesion_id: Optional[int] = None
    tarea_codigo: Optional[str] = None
    frente: Optional[str] = None
    mensaje: str
    payload: Optional[dict] = None


class DiarioIn(BaseModel):
    frente: Optional[str] = None
    autor: str
    titulo: Optional[str] = None
    contenido: str
    tags: Optional[str] = None


class DiarioPatch(BaseModel):
    titulo: Optional[str] = None
    contenido: Optional[str] = None
    tags: Optional[str] = None


class ArchitectMessageIn(BaseModel):
    role: str = "user"
    content: str
    session_id: Optional[str] = None
    metadata: Optional[dict] = None


class AssignmentIn(BaseModel):
    message_id: int
    task_codigo: str
    model: Optional[str] = None
    hint: Optional[str] = None
    status: str = "pending"


class AssignmentUpdate(BaseModel):
    status: str = Field(..., pattern="^(approved|rejected)$")


# ============================================================================
# STARTUP — init DB if needed
# ============================================================================

@app.on_event("startup")
def startup():
    db_path = Path(DB_PATH)
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path = Path(__file__).parent / "schema.sql"
        if schema_path.exists():
            con = sqlite3.connect(str(db_path))
            con.executescript(schema_path.read_text(encoding="utf-8"))
            con.close()


# ============================================================================
# 1. FRENTES
# ============================================================================

@app.get("/v1/frentes")
def listar_frentes(estado: Optional[str] = None):
    with db() as con:
        q = "SELECT * FROM frentes"
        params = []
        if estado:
            q += " WHERE estado = ?"
            params.append(estado)
        q += " ORDER BY (estado='active') DESC, nombre"
        return rows_to_list(con.execute(q, params))


@app.get("/v1/frentes/{slug}")
def obtener_frente(slug: str):
    with db() as con:
        row = con.execute("SELECT * FROM frentes WHERE slug = ?", (slug,)).fetchone()
        if not row:
            err(404, "frente_not_found", f"Frente '{slug}' not found")
        frente = dict(row)
        m = con.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM objetivos WHERE frente_id=?) AS objetivos_total,
              (SELECT COUNT(*) FROM objetivos WHERE frente_id=? AND estado='done') AS objetivos_done,
              (SELECT COUNT(*) FROM tareas WHERE frente_id=?) AS tareas_total,
              (SELECT COUNT(*) FROM tareas WHERE frente_id=? AND estado='available') AS tareas_disponibles,
              (SELECT COUNT(*) FROM tareas WHERE frente_id=? AND estado='in_progress') AS tareas_en_progreso,
              (SELECT COUNT(*) FROM tareas WHERE frente_id=? AND estado='done') AS tareas_done,
              (SELECT COUNT(*) FROM tareas WHERE frente_id=? AND estado='blocked') AS tareas_blocked,
              (SELECT COUNT(*) FROM sesiones WHERE frente_id=? AND estado='active') AS sesiones_activas
            """,
            (frente["id"],) * 8,
        ).fetchone()
        frente["metricas"] = dict(m)
        return frente


@app.post("/v1/frentes", status_code=201)
def crear_frente(f: FrenteIn):
    with db() as con:
        try:
            cur = con.execute(
                """INSERT INTO frentes
                   (slug,nombre,descripcion,color,icono,repo_path,worktree_path,branch)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (f.slug, f.nombre, f.descripcion, f.color, f.icono,
                 f.repo_path, f.worktree_path, f.branch),
            )
        except sqlite3.IntegrityError:
            err(409, "slug_duplicado", f"Frente '{f.slug}' already exists")
        return dict(con.execute("SELECT * FROM frentes WHERE id=?", (cur.lastrowid,)).fetchone())


@app.patch("/v1/frentes/{slug}")
def actualizar_frente(slug: str, p: FrentePatch):
    cambios = {k: v for k, v in p.model_dump().items() if v is not None}
    if not cambios:
        err(400, "validacion", "Nothing to update")
    with db() as con:
        existe = con.execute("SELECT id FROM frentes WHERE slug=?", (slug,)).fetchone()
        if not existe:
            err(404, "frente_not_found", f"Frente '{slug}' not found")
        sets = ", ".join(f"{k}=?" for k in cambios)
        con.execute(f"UPDATE frentes SET {sets} WHERE slug=?", (*cambios.values(), slug))
        return dict(con.execute("SELECT * FROM frentes WHERE slug=?", (slug,)).fetchone())


# ============================================================================
# 2. OBJETIVOS
# ============================================================================

@app.get("/v1/objetivos")
def listar_objetivos(frente: Optional[str] = None, estado: Optional[str] = None):
    with db() as con:
        q = """
            SELECT o.*, f.slug AS frente_slug,
              (SELECT COUNT(*) FROM tareas t WHERE t.objetivo_id=o.id) AS tareas_total,
              (SELECT COUNT(*) FROM tareas t WHERE t.objetivo_id=o.id AND t.estado='done') AS tareas_done,
              (SELECT COUNT(*) FROM tareas t WHERE t.objetivo_id=o.id AND t.estado='in_progress') AS tareas_in_progress,
              (SELECT COUNT(*) FROM tareas t WHERE t.objetivo_id=o.id AND t.estado='blocked') AS tareas_blocked
            FROM objetivos o
            JOIN frentes f ON f.id = o.frente_id
        """
        wheres, params = [], []
        if frente:
            wheres.append("f.slug = ?")
            params.append(frente)
        if estado:
            wheres.append("o.estado = ?")
            params.append(estado)
        if wheres:
            q += " WHERE " + " AND ".join(wheres)
        q += " ORDER BY o.prioridad DESC, o.creado_ts"
        results = []
        for row in con.execute(q, params):
            d = dict(row)
            total = d["tareas_total"]
            d["progreso"] = {
                "total": total,
                "done": d["tareas_done"],
                "in_progress": d["tareas_in_progress"],
                "blocked": d["tareas_blocked"],
                "porcentaje": round(100.0 * d["tareas_done"] / total, 1) if total else 0.0,
            }
            results.append(d)
        return results


@app.post("/v1/objetivos", status_code=201)
def crear_objetivo(o: ObjetivoIn):
    with db() as con:
        f = con.execute("SELECT id FROM frentes WHERE slug=?", (o.frente,)).fetchone()
        if not f:
            err(404, "frente_not_found", f"Frente '{o.frente}' not found")
        cur = con.execute(
            """INSERT INTO objetivos (frente_id,titulo,descripcion,prioridad,deadline)
               VALUES (?,?,?,?,?)""",
            (f["id"], o.titulo, o.descripcion, o.prioridad, o.deadline),
        )
        return dict(con.execute("SELECT * FROM objetivos WHERE id=?", (cur.lastrowid,)).fetchone())


@app.patch("/v1/objetivos/{id}")
def actualizar_objetivo(id: int, p: ObjetivoPatch):
    cambios = {k: v for k, v in p.model_dump().items() if v is not None}
    if not cambios:
        err(400, "validacion", "Nothing to update")
    with db() as con:
        existe = con.execute("SELECT id FROM objetivos WHERE id=?", (id,)).fetchone()
        if not existe:
            err(404, "objetivo_not_found", f"Objetivo {id} not found")
        sets = ", ".join(f"{k}=?" for k in cambios)
        con.execute(f"UPDATE objetivos SET {sets} WHERE id=?", (*cambios.values(), id))
        return dict(con.execute("SELECT * FROM objetivos WHERE id=?", (id,)).fetchone())


@app.delete("/v1/objetivos/{id}", status_code=204)
def borrar_objetivo(id: int):
    with db() as con:
        cur = con.execute("DELETE FROM objetivos WHERE id=?", (id,))
        if cur.rowcount == 0:
            err(404, "objetivo_not_found", f"Objetivo {id} not found")
    return Response(status_code=204)


# ============================================================================
# 3. TAREAS
# ============================================================================

def _tarea_full(con, codigo: str) -> Optional[dict]:
    row = con.execute(
        """
        SELECT t.*, f.slug AS frente_slug, o.titulo AS objetivo_titulo,
               bl.codigo AS blocker_tarea_codigo
        FROM tareas t
        JOIN frentes f ON f.id = t.frente_id
        LEFT JOIN objetivos o ON o.id = t.objetivo_id
        LEFT JOIN tareas bl ON bl.id = t.blocker_tarea_id
        WHERE t.codigo = ?
        """,
        (codigo,),
    ).fetchone()
    if not row:
        return None
    tarea = dict(row)
    lock = con.execute(
        """
        SELECT l.id AS lock_id, l.sesion_id, l.tomado_ts, l.expira_ts,
               s.nombre_tmux AS sesion_nombre, s.host,
               CAST((julianday(l.expira_ts) - julianday('now')) * 1440 AS INTEGER) AS minutos_para_expirar
        FROM locks l
        JOIN sesiones s ON s.id = l.sesion_id
        WHERE l.tarea_id = ?
        """,
        (tarea["id"],),
    ).fetchone()
    tarea["lock"] = dict(lock) if lock else None
    tarea["modelo_efectivo"] = modelo_efectivo(tarea)
    return tarea


@app.get("/v1/tareas")
def listar_tareas(
    frente: Optional[str] = None,
    estado: Optional[str] = None,
    objetivo_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = 0,
):
    with db() as con:
        q = "SELECT t.codigo FROM tareas t JOIN frentes f ON f.id=t.frente_id"
        wheres, params = [], []
        if frente:
            wheres.append("f.slug = ?")
            params.append(frente)
        if estado:
            estados = estado.split(",")
            wheres.append(f"t.estado IN ({','.join('?' for _ in estados)})")
            params.extend(estados)
        if objetivo_id is not None:
            wheres.append("t.objetivo_id = ?")
            params.append(objetivo_id)
        if wheres:
            q += " WHERE " + " AND ".join(wheres)
        q += " ORDER BY t.prioridad DESC, t.creado_ts ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        codigos = [r["codigo"] for r in con.execute(q, params)]
        return [_tarea_full(con, c) for c in codigos]


@app.get("/v1/tareas/{codigo}")
def obtener_tarea(codigo: str):
    with db() as con:
        t = _tarea_full(con, codigo)
        if not t:
            err(404, "tarea_not_found", f"Task '{codigo}' not found")
        return t


@app.post("/v1/tareas", status_code=201)
def crear_tarea(t: TareaIn):
    with db() as con:
        f = con.execute("SELECT id FROM frentes WHERE slug=?", (t.frente,)).fetchone()
        if not f:
            err(404, "frente_not_found", f"Frente '{t.frente}' not found")
        blocker_id = None
        if t.blocker_tarea_codigo:
            br = con.execute("SELECT id FROM tareas WHERE codigo=?", (t.blocker_tarea_codigo,)).fetchone()
            if not br:
                err(404, "tarea_not_found", f"Blocker '{t.blocker_tarea_codigo}' not found")
            blocker_id = br["id"]
        try:
            con.execute(
                """INSERT INTO tareas
                   (frente_id,objetivo_id,codigo,titulo,descripcion,prioridad,
                    blocker_tarea_id,archivos_glob,estimacion_min,modelo_sugerido)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (f["id"], t.objetivo_id, t.codigo, t.titulo, t.descripcion,
                 t.prioridad, blocker_id, t.archivos_glob, t.estimacion_min, t.modelo_sugerido),
            )
        except sqlite3.IntegrityError as e:
            err(409, "codigo_duplicado", f"Task '{t.codigo}' already exists ({e})")
        return _tarea_full(con, t.codigo)


TRANSICIONES_VALIDAS = {
    "available":   {"locked", "in_progress", "cancelled", "blocked"},
    "locked":      {"available", "in_progress", "review", "done", "blocked", "cancelled"},
    "in_progress": {"locked", "review", "done", "blocked", "cancelled"},
    "review":      {"in_progress", "done", "cancelled"},
    "blocked":     {"available", "in_progress", "cancelled"},
    "done":        set(),
    "cancelled":   set(),
}


@app.patch("/v1/tareas/{codigo}")
def actualizar_tarea(codigo: str, p: TareaPatch):
    cambios = {k: v for k, v in p.model_dump().items() if v is not None}
    if not cambios:
        err(400, "validacion", "Nothing to update")
    with db() as con:
        actual = con.execute("SELECT * FROM tareas WHERE codigo=?", (codigo,)).fetchone()
        if not actual:
            err(404, "tarea_not_found", f"Task '{codigo}' not found")
        if "estado" in cambios:
            nuevo = cambios["estado"]
            permitidos = TRANSICIONES_VALIDAS.get(actual["estado"], set())
            if nuevo != actual["estado"] and nuevo not in permitidos:
                err(409, "tarea_estado_invalido",
                    f"Cannot transition from '{actual['estado']}' to '{nuevo}'")
        if "blocker_tarea_codigo" in cambios:
            bcod = cambios.pop("blocker_tarea_codigo")
            if bcod:
                br = con.execute("SELECT id FROM tareas WHERE codigo=?", (bcod,)).fetchone()
                if not br:
                    err(404, "tarea_not_found", f"Blocker '{bcod}' not found")
                cambios["blocker_tarea_id"] = br["id"]
            else:
                cambios["blocker_tarea_id"] = None
        sets = ", ".join(f"{k}=?" for k in cambios)
        con.execute(f"UPDATE tareas SET {sets} WHERE codigo=?", (*cambios.values(), codigo))
        return _tarea_full(con, codigo)


@app.delete("/v1/tareas/{codigo}", status_code=204)
def borrar_tarea(codigo: str):
    with db() as con:
        t = con.execute("SELECT id FROM tareas WHERE codigo=?", (codigo,)).fetchone()
        if not t:
            err(404, "tarea_not_found", f"Task '{codigo}' not found")
        l = con.execute("SELECT id FROM locks WHERE tarea_id=?", (t["id"],)).fetchone()
        if l:
            err(409, "lock_already_held", "Cannot delete task with active lock")
        con.execute("DELETE FROM tareas WHERE codigo=?", (codigo,))
    return Response(status_code=204)


# ============================================================================
# 4. SESIONES
# ============================================================================

@app.get("/v1/sesiones")
def listar_sesiones(estado: Optional[str] = None, frente: Optional[str] = None):
    with db() as con:
        q = "SELECT * FROM v_sesiones_activas WHERE 1=1"
        params = []
        if frente:
            q += " AND frente_slug = ?"
            params.append(frente)
        if estado:
            estados = estado.split(",")
            q += f" AND estado IN ({','.join('?' for _ in estados)})"
            params.extend(estados)
        q += " ORDER BY frente_slug, iniciada_ts DESC"
        return rows_to_list(con.execute(q, params))


def _tareas_disponibles_frente(con, frente_slug: str, limit: int = 10) -> list[dict]:
    rows = con.execute(
        """
        SELECT t.codigo, t.titulo, t.prioridad, t.estimacion_min, t.descripcion,
               t.modelo_sugerido
        FROM tareas t
        JOIN frentes f ON f.id = t.frente_id
        LEFT JOIN tareas bl ON bl.id = t.blocker_tarea_id
        WHERE f.slug = ?
          AND t.estado = 'available'
          AND (bl.id IS NULL OR bl.estado = 'done')
        ORDER BY t.prioridad DESC, t.creado_ts ASC
        LIMIT ?
        """,
        (frente_slug, limit),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["modelo_efectivo"] = modelo_efectivo(d)
        result.append(d)
    return result


@app.post("/v1/sesiones", status_code=201)
def registrar_sesion(s: SesionIn):
    import json
    with db() as con:
        f = con.execute("SELECT id FROM frentes WHERE slug=?", (s.frente,)).fetchone()
        if not f:
            err(404, "frente_not_found", f"Frente '{s.frente}' not found")
        try:
            cur = con.execute(
                """INSERT INTO sesiones (nombre_tmux,frente_id,host,pid,pwd,metadatos)
                   VALUES (?,?,?,?,?,?)""",
                (s.nombre_tmux, f["id"], s.host, s.pid, s.pwd,
                 json.dumps(s.metadatos) if s.metadatos else None),
            )
        except sqlite3.IntegrityError:
            err(409, "sesion_duplicada", f"Session '{s.nombre_tmux}' already exists")
        sid = cur.lastrowid
        con.execute(
            "INSERT INTO eventos (tipo, sesion_id, frente_id, mensaje) VALUES (?,?,?,?)",
            ("session_started", sid, f["id"], f"Session {s.nombre_tmux} started on {s.host}"),
        )
        sesion = dict(con.execute("SELECT * FROM v_sesiones_activas WHERE id=?", (sid,)).fetchone())
        sesion["tareas_disponibles"] = _tareas_disponibles_frente(con, s.frente, 10)
        return sesion


@app.post("/v1/sesiones/{sid}/heartbeat")
def heartbeat(sid: int, ttl_min: int = Query(30, ge=1, le=120)):
    with db() as con:
        s = con.execute("SELECT * FROM sesiones WHERE id=?", (sid,)).fetchone()
        if not s:
            err(404, "sesion_not_found", f"Session {sid} not found")
        if s["estado"] in ("closed", "crashed"):
            err(409, "sesion_ya_cerrada", f"Session {sid} is {s['estado']}")
        con.execute(
            """UPDATE sesiones
               SET ultimo_heartbeat_ts = datetime('now'),
                   estado = CASE WHEN estado='idle' THEN 'active' ELSE estado END
               WHERE id = ?""",
            (sid,),
        )
        cur = con.execute(
            f"UPDATE locks SET expira_ts = datetime('now', '+{ttl_min} minutes') WHERE sesion_id = ?",
            (sid,),
        )
        renovados = cur.rowcount
        con.execute(
            "INSERT INTO eventos (tipo, sesion_id, frente_id, payload) VALUES (?,?,?,?)",
            ("session_heartbeat", sid, s["frente_id"],
             f'{{"locks_renovados": {renovados}, "ttl_min": {ttl_min}}}'),
        )
        return {
            "id": sid,
            "ultimo_heartbeat_ts": con.execute(
                "SELECT ultimo_heartbeat_ts FROM sesiones WHERE id=?", (sid,)
            ).fetchone()[0],
            "locks_renovados": renovados,
            "ttl_min": ttl_min,
        }


@app.post("/v1/sesiones/{sid}/cerrar", status_code=204)
def cerrar_sesion(sid: int, c: CierreSesion):
    with db() as con:
        s = con.execute("SELECT * FROM sesiones WHERE id=?", (sid,)).fetchone()
        if not s:
            err(404, "sesion_not_found", f"Session {sid} not found")
        if s["estado"] in ("closed", "crashed"):
            return Response(status_code=204)
        con.execute("UPDATE sesiones SET estado='closed' WHERE id=?", (sid,))
        if c.nota_diario:
            con.execute(
                """INSERT INTO diario (frente_id, autor, titulo, contenido, tags)
                   VALUES (?,?,?,?,?)""",
                (s["frente_id"], f"agent:{s['nombre_tmux']}",
                 f"Session closed: {s['nombre_tmux']}",
                 c.nota_diario, "close,auto"),
            )
    return Response(status_code=204)


# ============================================================================
# 5. LOCKS
# ============================================================================

@app.get("/v1/locks")
def listar_locks(sesion_id: Optional[int] = None, frente: Optional[str] = None):
    with db() as con:
        q = """
            SELECT l.id AS lock_id, l.tarea_id, l.sesion_id, l.tomado_ts, l.expira_ts, l.notas,
                   t.codigo AS tarea_codigo, t.titulo AS tarea_titulo,
                   s.nombre_tmux AS sesion_nombre, s.host,
                   f.slug AS frente_slug
            FROM locks l
            JOIN tareas t ON t.id = l.tarea_id
            JOIN sesiones s ON s.id = l.sesion_id
            JOIN frentes f ON f.id = t.frente_id
            WHERE 1=1
        """
        params = []
        if sesion_id:
            q += " AND l.sesion_id = ?"
            params.append(sesion_id)
        if frente:
            q += " AND f.slug = ?"
            params.append(frente)
        q += " ORDER BY l.expira_ts ASC"
        return rows_to_list(con.execute(q, params))


@app.post("/v1/locks", status_code=201)
def tomar_lock(lk: LockIn):
    with db() as con:
        t = con.execute("SELECT * FROM tareas WHERE codigo=?", (lk.tarea_codigo,)).fetchone()
        if not t:
            err(404, "tarea_not_found", f"Task '{lk.tarea_codigo}' not found")
        s = con.execute("SELECT * FROM sesiones WHERE id=?", (lk.sesion_id,)).fetchone()
        if not s:
            err(404, "sesion_not_found", f"Session {lk.sesion_id} not found")
        if s["estado"] in ("closed", "crashed"):
            err(409, "sesion_ya_cerrada", f"Session {lk.sesion_id} is {s['estado']}")
        if t["estado"] in ("done", "cancelled"):
            err(409, "tarea_estado_invalido", f"Task is in terminal state '{t['estado']}'")
        existing = con.execute("SELECT * FROM locks WHERE tarea_id=?", (t["id"],)).fetchone()
        if existing:
            if existing["sesion_id"] == lk.sesion_id:
                con.execute(
                    f"UPDATE locks SET expira_ts=datetime('now','+{lk.ttl_min} minutes'),"
                    f" notas=COALESCE(?,notas) WHERE id=?",
                    (lk.notas, existing["id"]),
                )
                return dict(con.execute("SELECT * FROM locks WHERE id=?", (existing["id"],)).fetchone())
            else:
                duenio = con.execute(
                    "SELECT nombre_tmux FROM sesiones WHERE id=?", (existing["sesion_id"],)
                ).fetchone()
                err(409, "lock_already_held",
                    f"Task '{lk.tarea_codigo}' already locked by session '{duenio['nombre_tmux']}'")
        if t["blocker_tarea_id"]:
            bl = con.execute(
                "SELECT estado, codigo FROM tareas WHERE id=?", (t["blocker_tarea_id"],)
            ).fetchone()
            if bl["estado"] != "done":
                err(409, "tarea_bloqueada_por_dependencia",
                    f"Blocker '{bl['codigo']}' is '{bl['estado']}', not 'done'")
        cur = con.execute(
            f"""INSERT INTO locks (tarea_id, sesion_id, expira_ts, notas)
                VALUES (?, ?, datetime('now','+{lk.ttl_min} minutes'), ?)""",
            (t["id"], lk.sesion_id, lk.notas),
        )
        return dict(con.execute("SELECT * FROM locks WHERE id=?", (cur.lastrowid,)).fetchone())


@app.delete("/v1/locks/{lid}", status_code=204)
def liberar_lock(lid: int, sesion_id: int = Query(...)):
    with db() as con:
        l = con.execute("SELECT * FROM locks WHERE id=?", (lid,)).fetchone()
        if not l:
            err(404, "lock_not_found", f"Lock {lid} not found")
        if l["sesion_id"] != sesion_id:
            err(403, "lock_owner_mismatch",
                f"Session {sesion_id} does not own lock {lid}")
        con.execute("DELETE FROM locks WHERE id=?", (lid,))
    return Response(status_code=204)


@app.post("/v1/locks/{lid}/renovar")
def renovar_lock(lid: int, sesion_id: int = Query(...), ttl_min: int = Query(30, ge=1, le=120)):
    with db() as con:
        l = con.execute("SELECT * FROM locks WHERE id=?", (lid,)).fetchone()
        if not l:
            err(404, "lock_not_found", f"Lock {lid} not found")
        if l["sesion_id"] != sesion_id:
            err(403, "lock_owner_mismatch", f"Session {sesion_id} does not own this lock")
        con.execute(
            f"UPDATE locks SET expira_ts=datetime('now','+{ttl_min} minutes') WHERE id=?",
            (lid,),
        )
        return dict(con.execute("SELECT * FROM locks WHERE id=?", (lid,)).fetchone())


# ============================================================================
# 6. EVENTOS
# ============================================================================

@app.get("/v1/eventos")
def listar_eventos(
    frente: Optional[str] = None,
    sesion_id: Optional[int] = None,
    tarea_codigo: Optional[str] = None,
    tipo: Optional[str] = None,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
):
    with db() as con:
        q = """
            SELECT e.*, s.nombre_tmux AS sesion_nombre, f.slug AS frente_slug,
                   t.codigo AS tarea_codigo
            FROM eventos e
            LEFT JOIN sesiones s ON s.id = e.sesion_id
            LEFT JOIN frentes f ON f.id = e.frente_id
            LEFT JOIN tareas t ON t.id = e.tarea_id
            WHERE 1=1
        """
        params = []
        if frente:
            q += " AND f.slug = ?"; params.append(frente)
        if sesion_id:
            q += " AND e.sesion_id = ?"; params.append(sesion_id)
        if tarea_codigo:
            q += " AND t.codigo = ?"; params.append(tarea_codigo)
        if tipo:
            q += " AND e.tipo = ?"; params.append(tipo)
        if desde:
            q += " AND e.ts >= ?"; params.append(desde)
        if hasta:
            q += " AND e.ts <= ?"; params.append(hasta)
        q += " ORDER BY e.ts DESC LIMIT ?"
        params.append(limit)
        return rows_to_list(con.execute(q, params))


@app.get("/v1/eventos/stream")
async def stream_eventos(tarea_codigo: str, hidrate: int = Query(50, ge=0, le=500)):
    """SSE: pushes events for a task as they appear."""
    with db() as con:
        row = con.execute("SELECT id FROM tareas WHERE codigo=?", (tarea_codigo,)).fetchone()
        if not row:
            err(404, "tarea_not_found", f"Task '{tarea_codigo}' not found")
        tarea_id = row["id"]

    async def gen():
        last_id = 0
        with db() as con:
            initial = con.execute(
                """SELECT e.*, s.nombre_tmux AS sesion_nombre, f.slug AS frente_slug,
                          t.codigo AS tarea_codigo
                   FROM (SELECT * FROM eventos
                         WHERE tarea_id=? ORDER BY id DESC LIMIT ?) AS e
                   LEFT JOIN sesiones s ON s.id = e.sesion_id
                   LEFT JOIN frentes f ON f.id = e.frente_id
                   LEFT JOIN tareas t ON t.id = e.tarea_id
                   ORDER BY e.id ASC""",
                (tarea_id, hidrate),
            ).fetchall()
        for r in initial:
            d = dict(r)
            last_id = max(last_id, d["id"])
            yield f"data: {_json.dumps(d, default=str)}\n\n"

        while True:
            try:
                with db() as con:
                    rows = con.execute(
                        """SELECT e.*, s.nombre_tmux AS sesion_nombre, f.slug AS frente_slug,
                                  t.codigo AS tarea_codigo
                           FROM eventos e
                           LEFT JOIN sesiones s ON s.id = e.sesion_id
                           LEFT JOIN frentes f ON f.id = e.frente_id
                           LEFT JOIN tareas t ON t.id = e.tarea_id
                           WHERE e.tarea_id=? AND e.id > ?
                           ORDER BY e.id ASC""",
                        (tarea_id, last_id),
                    ).fetchall()
                for r in rows:
                    d = dict(r)
                    last_id = d["id"]
                    yield f"data: {_json.dumps(d, default=str)}\n\n"
                yield ": keepalive\n\n"
            except Exception as e:
                yield f": db_error {type(e).__name__}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/v1/eventos", status_code=201)
def crear_evento(e: EventoIn):
    import json
    if e.tipo != "note":
        err(400, "validacion", "Only 'note' type events are accepted via API")
    with db() as con:
        tarea_id = None
        frente_id = None
        if e.tarea_codigo:
            t = con.execute("SELECT id, frente_id FROM tareas WHERE codigo=?", (e.tarea_codigo,)).fetchone()
            if not t:
                err(404, "tarea_not_found", f"Task '{e.tarea_codigo}' not found")
            tarea_id = t["id"]
            frente_id = t["frente_id"]
        if e.frente and not frente_id:
            f = con.execute("SELECT id FROM frentes WHERE slug=?", (e.frente,)).fetchone()
            if not f:
                err(404, "frente_not_found", f"Frente '{e.frente}' not found")
            frente_id = f["id"]
        cur = con.execute(
            """INSERT INTO eventos (tipo,sesion_id,frente_id,tarea_id,mensaje,payload)
               VALUES (?,?,?,?,?,?)""",
            (e.tipo, e.sesion_id, frente_id, tarea_id, e.mensaje,
             json.dumps(e.payload) if e.payload else None),
        )
        return dict(con.execute("SELECT * FROM eventos WHERE id=?", (cur.lastrowid,)).fetchone())


# ============================================================================
# 7. DIARIO
# ============================================================================

@app.get("/v1/diario")
def listar_diario(
    frente: Optional[str] = None,
    fecha: Optional[str] = None,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    tags: Optional[str] = None,
    autor: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
):
    with db() as con:
        sql = """
            SELECT d.*, f.slug AS frente_slug
            FROM diario d
            LEFT JOIN frentes f ON f.id = d.frente_id
            WHERE 1=1
        """
        params = []
        if frente:
            sql += " AND f.slug = ?"; params.append(frente)
        if fecha:
            sql += " AND d.fecha = ?"; params.append(fecha)
        if desde:
            sql += " AND d.fecha >= ?"; params.append(desde)
        if hasta:
            sql += " AND d.fecha <= ?"; params.append(hasta)
        if tags:
            for tag in tags.split(","):
                sql += " AND d.tags LIKE ?"; params.append(f"%{tag.strip()}%")
        if autor:
            sql += " AND d.autor = ?"; params.append(autor)
        if q:
            sql += " AND (d.titulo LIKE ? OR d.contenido LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])
        sql += " ORDER BY d.ts DESC LIMIT ?"
        params.append(limit)
        return rows_to_list(con.execute(sql, params))


@app.get("/v1/diario/{id}")
def obtener_diario(id: int):
    with db() as con:
        row = con.execute(
            """SELECT d.*, f.slug AS frente_slug
               FROM diario d LEFT JOIN frentes f ON f.id=d.frente_id
               WHERE d.id=?""",
            (id,),
        ).fetchone()
        if not row:
            err(404, "diario_not_found", f"Entry {id} not found")
        return dict(row)


@app.post("/v1/diario", status_code=201)
def crear_diario(d: DiarioIn):
    with db() as con:
        frente_id = None
        if d.frente:
            f = con.execute("SELECT id FROM frentes WHERE slug=?", (d.frente,)).fetchone()
            if not f:
                err(404, "frente_not_found", f"Frente '{d.frente}' not found")
            frente_id = f["id"]
        cur = con.execute(
            """INSERT INTO diario (frente_id,autor,titulo,contenido,tags)
               VALUES (?,?,?,?,?)""",
            (frente_id, d.autor, d.titulo, d.contenido, d.tags),
        )
        return dict(con.execute("SELECT * FROM diario WHERE id=?", (cur.lastrowid,)).fetchone())


@app.patch("/v1/diario/{id}")
def actualizar_diario(id: int, p: DiarioPatch):
    cambios = {k: v for k, v in p.model_dump().items() if v is not None}
    if not cambios:
        err(400, "validacion", "Nothing to update")
    with db() as con:
        existe = con.execute("SELECT id FROM diario WHERE id=?", (id,)).fetchone()
        if not existe:
            err(404, "diario_not_found", f"Entry {id} not found")
        sets = ", ".join(f"{k}=?" for k in cambios)
        con.execute(f"UPDATE diario SET {sets} WHERE id=?", (*cambios.values(), id))
        return dict(con.execute("SELECT * FROM diario WHERE id=?", (id,)).fetchone())


@app.delete("/v1/diario/{id}", status_code=204)
def borrar_diario(id: int):
    with db() as con:
        cur = con.execute("DELETE FROM diario WHERE id=?", (id,))
        if cur.rowcount == 0:
            err(404, "diario_not_found", f"Entry {id} not found")
    return Response(status_code=204)


# ============================================================================
# 8. SKILLS
# ============================================================================

@app.get("/v1/skills")
def listar_skills(categoria: Optional[str] = None, tag: Optional[str] = None, activa: int = 1):
    with db() as con:
        q = "SELECT * FROM skills WHERE activa = ?"
        params = [activa]
        if categoria:
            q += " AND categoria = ?"; params.append(categoria)
        if tag:
            q += " AND tags LIKE ?"; params.append(f"%{tag}%")
        q += " ORDER BY categoria, nombre"
        return rows_to_list(con.execute(q, params))


@app.get("/v1/skills/{slug}/contenido", response_class=PlainTextResponse)
def obtener_skill_contenido(slug: str):
    with db() as con:
        row = con.execute("SELECT path_skill_md FROM skills WHERE slug=?", (slug,)).fetchone()
        if not row:
            err(404, "skill_not_found", f"Skill '{slug}' not found")
        path = Path(row["path_skill_md"])
        if not path.is_file():
            err(404, "skill_path_invalido", f"File not found: {path}")
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown")


@app.post("/v1/skills/reindex")
def reindex_skills():
    descubiertas = 0
    actualizadas = 0
    desaparecidas = 0
    with db() as con:
        encontradas_path = set()
        for base in SKILLS_DIRS:
            base_path = Path(base).expanduser()
            if not base_path.is_dir():
                continue
            for skill_md in base_path.rglob("SKILL.md"):
                encontradas_path.add(str(skill_md))
                slug = skill_md.parent.name
                mtime = datetime.fromtimestamp(skill_md.stat().st_mtime).isoformat(sep=" ")
                try:
                    head = skill_md.read_text(encoding="utf-8").splitlines()[:20]
                    nombre = next((l.lstrip("# ").strip() for l in head if l.startswith("#")), slug)
                    descripcion = next(
                        (l.strip() for l in head if l.strip() and not l.startswith("#")),
                        None,
                    )
                except Exception:
                    nombre = slug
                    descripcion = None
                existe = con.execute("SELECT id FROM skills WHERE path_skill_md=?", (str(skill_md),)).fetchone()
                if existe:
                    con.execute(
                        """UPDATE skills SET nombre=?, descripcion=?, ultima_modif_ts=?, activa=1
                           WHERE id=?""",
                        (nombre, descripcion, mtime, existe["id"]),
                    )
                    actualizadas += 1
                else:
                    con.execute(
                        """INSERT INTO skills (slug,nombre,descripcion,path_skill_md,ultima_modif_ts,activa)
                           VALUES (?,?,?,?,?,1)""",
                        (slug, nombre, descripcion, str(skill_md), mtime),
                    )
                    descubiertas += 1
        existing = con.execute("SELECT id, path_skill_md FROM skills WHERE activa=1").fetchall()
        for row in existing:
            if row["path_skill_md"] not in encontradas_path:
                con.execute("UPDATE skills SET activa=0 WHERE id=?", (row["id"],))
                desaparecidas += 1
        total = con.execute("SELECT COUNT(*) FROM skills WHERE activa=1").fetchone()[0]
        return {
            "descubiertas": descubiertas,
            "actualizadas": actualizadas,
            "desaparecidas": desaparecidas,
            "total_activas": total,
        }


# ============================================================================
# 9. PLAN
# ============================================================================

@app.get("/v1/plan")
def plan_estructurado():
    with db() as con:
        frentes = rows_to_list(con.execute(
            "SELECT * FROM frentes WHERE estado='active' ORDER BY nombre"
        ))
        for f in frentes:
            objetivos = rows_to_list(con.execute(
                """SELECT * FROM v_objetivo_progreso WHERE frente_slug=? ORDER BY prioridad DESC""",
                (f["slug"],),
            ))
            for o in objetivos:
                o["tareas"] = rows_to_list(con.execute(
                    """SELECT codigo, titulo, tarea_estado AS estado,
                              lock_owner, lock_expira_ts,
                              blocker_codigo, blocker_estado, prioridad
                       FROM v_plan_maestro
                       WHERE frente_slug=? AND objetivo_titulo=?
                       ORDER BY prioridad DESC, codigo""",
                    (f["slug"], o["titulo"]),
                ))
            f["objetivos"] = objetivos
            f["tareas_sin_objetivo"] = rows_to_list(con.execute(
                """SELECT t.codigo, t.titulo, t.estado, t.prioridad,
                          s.nombre_tmux AS lock_owner
                   FROM tareas t
                   JOIN frentes fr ON fr.id=t.frente_id
                   LEFT JOIN locks l ON l.tarea_id=t.id
                   LEFT JOIN sesiones s ON s.id=l.sesion_id
                   WHERE fr.slug=? AND t.objetivo_id IS NULL
                     AND t.estado NOT IN ('done','cancelled')
                   ORDER BY t.prioridad DESC, t.codigo""",
                (f["slug"],),
            ))
        return {
            "generado_ts": datetime.utcnow().isoformat(sep=" ", timespec="seconds"),
            "frentes": frentes,
        }


@app.get("/v1/plan/snapshot", response_class=PlainTextResponse)
def plan_snapshot():
    plan = plan_estructurado()
    lines = [f"# Master Plan — {plan['generado_ts']}", ""]
    for f in plan["frentes"]:
        lines.append(f"## {f['slug']} — {f['nombre']}")
        if f.get("descripcion"):
            lines.append(f"_{f['descripcion']}_")
        lines.append("")
        for o in f["objetivos"]:
            pct = o["porcentaje_done"]
            lines.append(f"### {o['titulo']}  ({pct}% — {o['total_tareas']} tasks)")
            for t in o["tareas"]:
                box = "[x]" if t["estado"] == "done" else "[ ]"
                extras = []
                if t["lock_owner"]:
                    extras.append(f"locked:{t['lock_owner']}")
                if t["blocker_codigo"] and t.get("blocker_estado") != "done":
                    extras.append(f"blocked by {t['blocker_codigo']}")
                if t["estado"] == "blocked":
                    extras.append("blocked")
                if t["estado"] == "in_progress":
                    extras.append("in progress")
                tail = ("   " + "  ".join(extras)) if extras else ""
                lines.append(f"- {box} {t['codigo']} — {t['titulo']}{tail}")
            lines.append("")
        if f["tareas_sin_objetivo"]:
            lines.append("### Unassigned tasks")
            for t in f["tareas_sin_objetivo"]:
                box = "[x]" if t["estado"] == "done" else "[ ]"
                tail = f"   locked:{t['lock_owner']}" if t["lock_owner"] else ""
                lines.append(f"- {box} {t['codigo']} — {t['titulo']}{tail}")
            lines.append("")
    return PlainTextResponse("\n".join(lines), media_type="text/markdown")


@app.get("/v1/sesion/disponibles")
def tareas_disponibles(frente: str, limit: int = Query(10, ge=1, le=50)):
    with db() as con:
        if not con.execute("SELECT id FROM frentes WHERE slug=?", (frente,)).fetchone():
            err(404, "frente_not_found", f"Frente '{frente}' not found")
        return _tareas_disponibles_frente(con, frente, limit)


# ============================================================================
# 10. HEALTH & META
# ============================================================================

@app.get("/v1/health")
def health():
    try:
        with db() as con:
            con.execute("SELECT 1").fetchone()
        return {
            "status": "ok",
            "db_path": DB_PATH,
            "db_size_mb": round(os.path.getsize(DB_PATH) / 1024 / 1024, 2)
                if os.path.exists(DB_PATH) else 0,
            "uptime_s": int(time.time() - START_TS),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.get("/v1/stats")
def stats():
    with db() as con:
        bib_count = 0
        try:
            bib_count = con.execute("SELECT COUNT(*) FROM biblioteca").fetchone()[0]
        except sqlite3.OperationalError:
            pass
        return {
            "frentes_activos": con.execute(
                "SELECT COUNT(*) FROM frentes WHERE estado='active'"
            ).fetchone()[0],
            "sesiones_activas": con.execute(
                "SELECT COUNT(*) FROM sesiones WHERE estado='active'"
            ).fetchone()[0],
            "locks_activos": con.execute("SELECT COUNT(*) FROM locks").fetchone()[0],
            "tareas_total": con.execute("SELECT COUNT(*) FROM tareas").fetchone()[0],
            "tareas_done": con.execute(
                "SELECT COUNT(*) FROM tareas WHERE estado='done'"
            ).fetchone()[0],
            "diario_entradas": con.execute("SELECT COUNT(*) FROM diario").fetchone()[0],
            "skills_activas": con.execute(
                "SELECT COUNT(*) FROM skills WHERE activa=1"
            ).fetchone()[0],
            "biblioteca_repos": bib_count,
        }


# ============================================================================
# 11. BIBLIOTECA
# ============================================================================

class BibliotecaIn(BaseModel):
    slug: str
    nombre: str
    descripcion: Optional[str] = None
    categoria: str
    lenguaje: Optional[str] = None
    estado: str = "activo"
    canonical_path: Optional[str] = None
    github: Optional[str] = None
    ultimo_commit: Optional[str] = None
    features: Optional[list[str]] = None
    key_files: Optional[list[str]] = None
    notas: Optional[str] = None
    frente_slug: Optional[str] = None


class BibliotecaPatch(BaseModel):
    nombre: Optional[str] = None
    descripcion: Optional[str] = None
    categoria: Optional[str] = None
    lenguaje: Optional[str] = None
    estado: Optional[str] = None
    canonical_path: Optional[str] = None
    github: Optional[str] = None
    ultimo_commit: Optional[str] = None
    features: Optional[list[str]] = None
    key_files: Optional[list[str]] = None
    notas: Optional[str] = None
    frente_slug: Optional[str] = None


def _bib_row_to_dict(row) -> dict:
    d = dict(row)
    for field in ("features", "key_files"):
        if d.get(field):
            try:
                d[field] = _json.loads(d[field])
            except (ValueError, TypeError):
                pass
    return d


@app.get("/v1/biblioteca")
def listar_biblioteca(
    categoria: Optional[str] = None,
    estado: Optional[str] = None,
    lenguaje: Optional[str] = None,
    q: Optional[str] = None,
    frente: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    with db() as con:
        sql = """
            SELECT b.*, f.slug AS frente_slug
            FROM biblioteca b
            LEFT JOIN frentes f ON f.id = b.frente_id
            WHERE 1=1
        """
        params = []
        if categoria:
            sql += " AND b.categoria = ?"
            params.append(categoria)
        if estado:
            sql += " AND b.estado = ?"
            params.append(estado)
        if lenguaje:
            sql += " AND b.lenguaje LIKE ?"
            params.append(f"%{lenguaje}%")
        if q:
            sql += " AND (b.nombre LIKE ? OR b.descripcion LIKE ? OR b.features LIKE ? OR b.notas LIKE ?)"
            params.extend([f"%{q}%"] * 4)
        if frente:
            sql += " AND f.slug = ?"
            params.append(frente)
        count_sql = sql.replace(
            "SELECT b.*, f.slug AS frente_slug",
            "SELECT COUNT(*) AS total",
        )
        total = con.execute(count_sql, params).fetchone()["total"]
        sql += " ORDER BY b.categoria, b.nombre LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        items = [_bib_row_to_dict(r) for r in con.execute(sql, params)]
        return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/v1/biblioteca/categorias")
def categorias_biblioteca():
    with db() as con:
        rows = con.execute(
            """SELECT categoria, COUNT(*) AS count,
                      SUM(CASE WHEN estado='activo' THEN 1 ELSE 0 END) AS activos,
                      SUM(CASE WHEN estado='legacy' THEN 1 ELSE 0 END) AS legacy
               FROM biblioteca GROUP BY categoria ORDER BY count DESC"""
        ).fetchall()
        return rows_to_list(rows)


@app.get("/v1/biblioteca/{slug}")
def obtener_biblioteca(slug: str):
    with db() as con:
        row = con.execute(
            """SELECT b.*, f.slug AS frente_slug
               FROM biblioteca b
               LEFT JOIN frentes f ON f.id = b.frente_id
               WHERE b.slug = ?""",
            (slug,),
        ).fetchone()
        if not row:
            err(404, "repo_not_found", f"Repo '{slug}' not found")
        return _bib_row_to_dict(row)


@app.post("/v1/biblioteca", status_code=201)
def crear_biblioteca(b: BibliotecaIn):
    with db() as con:
        frente_id = None
        if b.frente_slug:
            f = con.execute("SELECT id FROM frentes WHERE slug=?", (b.frente_slug,)).fetchone()
            if not f:
                err(404, "frente_not_found", f"Frente '{b.frente_slug}' not found")
            frente_id = f["id"]
        try:
            cur = con.execute(
                """INSERT INTO biblioteca
                   (slug,nombre,descripcion,categoria,lenguaje,estado,
                    canonical_path,github,ultimo_commit,features,key_files,notas,frente_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (b.slug, b.nombre, b.descripcion, b.categoria, b.lenguaje, b.estado,
                 b.canonical_path, b.github, b.ultimo_commit,
                 _json.dumps(b.features, ensure_ascii=False) if b.features else None,
                 _json.dumps(b.key_files, ensure_ascii=False) if b.key_files else None,
                 b.notas, frente_id),
            )
        except sqlite3.IntegrityError:
            err(409, "slug_duplicado", f"Repo '{b.slug}' already exists")
        return _bib_row_to_dict(
            con.execute("SELECT b.*, NULL AS frente_slug FROM biblioteca b WHERE id=?",
                        (cur.lastrowid,)).fetchone()
        )


@app.patch("/v1/biblioteca/{slug}")
def actualizar_biblioteca(slug: str, p: BibliotecaPatch):
    cambios = {k: v for k, v in p.model_dump().items() if v is not None}
    if not cambios:
        err(400, "validacion", "Nothing to update")
    with db() as con:
        existe = con.execute("SELECT id FROM biblioteca WHERE slug=?", (slug,)).fetchone()
        if not existe:
            err(404, "repo_not_found", f"Repo '{slug}' not found")
        if "frente_slug" in cambios:
            fs = cambios.pop("frente_slug")
            if fs:
                f = con.execute("SELECT id FROM frentes WHERE slug=?", (fs,)).fetchone()
                if not f:
                    err(404, "frente_not_found", f"Frente '{fs}' not found")
                cambios["frente_id"] = f["id"]
            else:
                cambios["frente_id"] = None
        for field in ("features", "key_files"):
            if field in cambios:
                cambios[field] = _json.dumps(cambios[field], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in cambios)
        con.execute(f"UPDATE biblioteca SET {sets} WHERE slug=?", (*cambios.values(), slug))
        return _bib_row_to_dict(
            con.execute(
                """SELECT b.*, f.slug AS frente_slug FROM biblioteca b
                   LEFT JOIN frentes f ON f.id=b.frente_id WHERE b.slug=?""",
                (slug,),
            ).fetchone()
        )


@app.delete("/v1/biblioteca/{slug}", status_code=204)
def borrar_biblioteca(slug: str):
    with db() as con:
        cur = con.execute("DELETE FROM biblioteca WHERE slug=?", (slug,))
        if cur.rowcount == 0:
            err(404, "repo_not_found", f"Repo '{slug}' not found")
    return Response(status_code=204)


@app.get("/v1/biblioteca/buscar/features")
def buscar_por_feature(q: str, limit: int = Query(20, ge=1, le=100)):
    with db() as con:
        rows = con.execute(
            """SELECT b.*, f.slug AS frente_slug
               FROM biblioteca b
               LEFT JOIN frentes f ON f.id = b.frente_id
               WHERE b.features LIKE ?
               ORDER BY b.estado = 'activo' DESC, b.nombre
               LIMIT ?""",
            (f"%{q}%", limit),
        ).fetchall()
        return [_bib_row_to_dict(r) for r in rows]


# ============================================================================
# ARCHITECT CHAT
# ============================================================================

@app.post("/v1/architect/messages", status_code=201)
def crear_architect_message(m: ArchitectMessageIn):
    if m.role not in ("user", "architect", "system"):
        err(400, "validacion", "role must be user, architect, or system")
    meta = _json.dumps(m.metadata, ensure_ascii=False) if m.metadata else None
    with db() as con:
        cur = con.execute(
            "INSERT INTO architect_messages (session_id, role, content, metadata) VALUES (?,?,?,?)",
            (m.session_id, m.role, m.content, meta),
        )
        row = con.execute("SELECT * FROM architect_messages WHERE id=?", (cur.lastrowid,)).fetchone()
        d = dict(row)
        if d.get("metadata"):
            try:
                d["metadata"] = _json.loads(d["metadata"])
            except (ValueError, TypeError):
                pass
        return d


@app.get("/v1/architect/messages")
def listar_architect_messages(
    since: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    session_id: Optional[str] = None,
):
    with db() as con:
        q = "SELECT * FROM architect_messages WHERE 1=1"
        params: list = []
        if since:
            q += " AND created_at > ?"
            params.append(since)
        if session_id:
            q += " AND session_id = ?"
            params.append(session_id)
        q += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        rows = rows_to_list(con.execute(q, params))
        for d in rows:
            if d.get("metadata"):
                try:
                    d["metadata"] = _json.loads(d["metadata"])
                except (ValueError, TypeError):
                    pass
        return rows


@app.get("/v1/architect/messages/stream")
async def stream_architect_messages(hydrate: int = Query(50, ge=0, le=500)):
    """SSE: pushes architect messages as they appear."""

    async def gen():
        last_id = 0
        with db() as con:
            initial = con.execute(
                "SELECT * FROM architect_messages ORDER BY id DESC LIMIT ?",
                (hydrate,),
            ).fetchall()
        for r in sorted(initial, key=lambda x: x["id"]):
            d = dict(r)
            last_id = max(last_id, d["id"])
            if d.get("metadata"):
                try:
                    d["metadata"] = _json.loads(d["metadata"])
                except (ValueError, TypeError):
                    pass
            yield f"data: {_json.dumps(d, default=str)}\n\n"

        while True:
            try:
                with db() as con:
                    rows = con.execute(
                        "SELECT * FROM architect_messages WHERE id > ? ORDER BY id ASC",
                        (last_id,),
                    ).fetchall()
                for r in rows:
                    d = dict(r)
                    last_id = d["id"]
                    if d.get("metadata"):
                        try:
                            d["metadata"] = _json.loads(d["metadata"])
                        except (ValueError, TypeError):
                            pass
                    yield f"data: {_json.dumps(d, default=str)}\n\n"
                yield ": keepalive\n\n"
            except Exception as e:
                yield f": db_error {type(e).__name__}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/v1/architect/assignments")
def listar_assignments(
    status: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    with db() as con:
        q = """SELECT a.*, m.content AS message_content, m.role AS message_role
               FROM architect_assignments a
               LEFT JOIN architect_messages m ON m.id = a.message_id
               WHERE 1=1"""
        params: list = []
        if status:
            q += " AND a.status = ?"
            params.append(status)
        q += " ORDER BY a.id DESC LIMIT ?"
        params.append(limit)
        return rows_to_list(con.execute(q, params))


@app.post("/v1/architect/assignments", status_code=201)
def crear_assignment(a: AssignmentIn):
    with db() as con:
        msg = con.execute("SELECT id FROM architect_messages WHERE id=?", (a.message_id,)).fetchone()
        if not msg:
            err(404, "message_not_found", f"Message {a.message_id} not found")
        cur = con.execute(
            "INSERT INTO architect_assignments (message_id, task_codigo, model, hint, status) VALUES (?,?,?,?,?)",
            (a.message_id, a.task_codigo, a.model, a.hint, a.status),
        )
        row = con.execute(
            """SELECT a.*, m.content AS message_content, m.role AS message_role
               FROM architect_assignments a
               LEFT JOIN architect_messages m ON m.id = a.message_id
               WHERE a.id=?""",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row)


@app.put("/v1/architect/assignments/{assignment_id}")
def actualizar_assignment(assignment_id: int, body: AssignmentUpdate):
    with db() as con:
        row = con.execute("SELECT * FROM architect_assignments WHERE id=?", (assignment_id,)).fetchone()
        if not row:
            err(404, "assignment_not_found", f"Assignment {assignment_id} not found")
        if row["status"] != "pending":
            err(409, "already_resolved", f"Assignment already {row['status']}")
        con.execute(
            "UPDATE architect_assignments SET status=? WHERE id=?",
            (body.status, assignment_id),
        )
        updated = con.execute(
            """SELECT a.*, m.content AS message_content, m.role AS message_role
               FROM architect_assignments a
               LEFT JOIN architect_messages m ON m.id = a.message_id
               WHERE a.id=?""",
            (assignment_id,),
        ).fetchone()
        return dict(updated)


@app.post("/v1/architect/invoke", status_code=202)
def invocar_architect(body: Optional[ArchitectMessageIn] = None):
    """
    Trigger an architect invocation. If body is provided, saves the message
    first. The orchestrator picks up the invoke flag on its next poll.
    """
    if body and body.content:
        meta = _json.dumps(body.metadata, ensure_ascii=False) if body.metadata else None
        with db() as con:
            con.execute(
                "INSERT INTO architect_messages (session_id, role, content, metadata) VALUES (?,?,?,?)",
                (body.session_id, "user", body.content, meta),
            )
    with db() as con:
        con.execute(
            "INSERT INTO eventos (tipo, mensaje) VALUES (?, ?)",
            ("architect_invoke", "Architect invocation requested from web"),
        )
    return {"status": "queued", "detail": "Architect will run on next orchestrator cycle"}


@app.get("/v1/architect/config")
def architect_config():
    return {
        "auto_approve": os.getenv("ARCHITECT_AUTO_APPROVE", "true").lower() == "true",
        "architect_model": os.getenv("ARCHITECT_MODEL", "opus"),
        "architect_budget_usd": float(os.getenv("ARCHITECT_BUDGET_USD", "1.5")),
    }
