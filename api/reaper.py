"""
Orchestra Console — Reaper

Daemon that periodically:
1. Releases locks whose expira_ts has passed (no heartbeat from session).
2. Marks as 'crashed' sessions whose last heartbeat is too old.
3. Transitions silent sessions to 'idle'.

Launch:
    python reaper.py
"""

import os
import sqlite3
import time
from datetime import datetime

DB_PATH = os.getenv("ORCHESTRA_DB", "/data/orchestra.db")
INTERVAL_S = int(os.getenv("REAPER_INTERVAL_S", "60"))
SESION_DEAD_MIN = int(os.getenv("REAPER_SESION_DEAD_MIN", "30"))


def log(msg: str):
    print(f"[{datetime.utcnow().isoformat(sep=' ', timespec='seconds')}] {msg}", flush=True)


def reap_once():
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        expirados = con.execute(
            """SELECT l.id, l.tarea_id, l.sesion_id, t.codigo AS tarea_codigo,
                      s.nombre_tmux AS sesion_nombre, l.expira_ts
               FROM locks l
               JOIN tareas t ON t.id = l.tarea_id
               JOIN sesiones s ON s.id = l.sesion_id
               WHERE l.expira_ts < datetime('now')"""
        ).fetchall()

        for lk in expirados:
            log(f"  expired lock: {lk['tarea_codigo']} (session {lk['sesion_nombre']}, expired {lk['expira_ts']})")
            con.execute(
                "INSERT INTO eventos (tipo, sesion_id, tarea_id, mensaje) VALUES (?,?,?,?)",
                ("lock_expired", lk["sesion_id"], lk["tarea_id"],
                 f"Lock expired, released by reaper (was {lk['expira_ts']})"),
            )
            con.execute("DELETE FROM locks WHERE id=?", (lk["id"],))

        zombis = con.execute(
            f"""SELECT id, nombre_tmux, ultimo_heartbeat_ts
                FROM sesiones
                WHERE estado IN ('active','idle')
                  AND (julianday('now') - julianday(ultimo_heartbeat_ts)) * 1440 > {SESION_DEAD_MIN}"""
        ).fetchall()

        for s in zombis:
            log(f"  zombie session: {s['nombre_tmux']} (last heartbeat {s['ultimo_heartbeat_ts']})")
            con.execute("UPDATE sesiones SET estado='crashed' WHERE id=?", (s["id"],))

        con.execute(
            f"""UPDATE sesiones SET estado='idle'
                WHERE estado='active'
                  AND (julianday('now') - julianday(ultimo_heartbeat_ts)) * 1440 BETWEEN 5 AND {SESION_DEAD_MIN}"""
        )

        con.commit()

        if expirados or zombis:
            log(f"reap: {len(expirados)} locks released, {len(zombis)} sessions marked crashed")
    finally:
        con.close()


def main():
    log(f"Reaper started. db={DB_PATH} interval={INTERVAL_S}s dead_min={SESION_DEAD_MIN}")
    while True:
        try:
            reap_once()
        except Exception as e:
            log(f"ERROR in reap_once: {e}")
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
