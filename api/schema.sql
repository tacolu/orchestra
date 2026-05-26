-- ============================================================================
-- Orchestra Console · esquema SQLite
-- ============================================================================
-- Source of truth para coordinación multi-instancia de agentes AI.
-- 9 tablas: frentes, objetivos, tareas, sesiones, locks, eventos, diario,
--           biblioteca, skills.
--
-- Ejecutar:
--   sqlite3 orchestra.db < schema.sql
-- ============================================================================


-- ─── PRAGMAs ─────────────────────────────────────────────────────────────────
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;


-- ============================================================================
-- TABLAS
-- ============================================================================


-- ─── frentes ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS frentes (
    id              INTEGER PRIMARY KEY,
    slug            TEXT    NOT NULL UNIQUE,
    nombre          TEXT    NOT NULL,
    descripcion     TEXT,
    color           TEXT,
    icono           TEXT,
    repo_path       TEXT,
    worktree_path   TEXT,
    branch          TEXT,
    estado          TEXT    NOT NULL DEFAULT 'active'
                    CHECK (estado IN ('active','paused','archived')),
    creado_ts       TEXT    NOT NULL DEFAULT (datetime('now')),
    actualizado_ts  TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- ─── objetivos ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS objetivos (
    id              INTEGER PRIMARY KEY,
    frente_id       INTEGER NOT NULL REFERENCES frentes(id) ON DELETE CASCADE,
    titulo          TEXT    NOT NULL,
    descripcion     TEXT,
    prioridad       INTEGER NOT NULL DEFAULT 0
                    CHECK (prioridad BETWEEN 0 AND 10),
    estado          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (estado IN ('pending','in_progress','done','blocked','cancelled')),
    deadline        TEXT,
    creado_ts       TEXT    NOT NULL DEFAULT (datetime('now')),
    actualizado_ts  TEXT    NOT NULL DEFAULT (datetime('now')),
    cerrado_ts      TEXT
);


-- ─── tareas ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tareas (
    id                  INTEGER PRIMARY KEY,
    frente_id           INTEGER NOT NULL REFERENCES frentes(id)   ON DELETE CASCADE,
    objetivo_id         INTEGER          REFERENCES objetivos(id) ON DELETE SET NULL,
    codigo              TEXT    NOT NULL UNIQUE,
    titulo              TEXT    NOT NULL,
    descripcion         TEXT,
    estado              TEXT    NOT NULL DEFAULT 'available'
                        CHECK (estado IN ('available','locked','in_progress','review','done','blocked','cancelled')),
    prioridad           INTEGER NOT NULL DEFAULT 0
                        CHECK (prioridad BETWEEN 0 AND 10),
    blocker_tarea_id    INTEGER REFERENCES tareas(id),
    archivos_glob       TEXT,
    estimacion_min      INTEGER,
    modelo_sugerido     TEXT,
    creado_ts           TEXT    NOT NULL DEFAULT (datetime('now')),
    actualizado_ts      TEXT    NOT NULL DEFAULT (datetime('now')),
    cerrado_ts          TEXT,

    CHECK (blocker_tarea_id IS NULL OR blocker_tarea_id != id)
);


-- ─── sesiones ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sesiones (
    id                  INTEGER PRIMARY KEY,
    nombre_tmux         TEXT    NOT NULL UNIQUE,
    frente_id           INTEGER          REFERENCES frentes(id) ON DELETE SET NULL,
    host                TEXT    NOT NULL,
    pid                 INTEGER,
    pwd                 TEXT,
    estado              TEXT    NOT NULL DEFAULT 'active'
                        CHECK (estado IN ('active','idle','crashed','closed')),
    iniciada_ts         TEXT    NOT NULL DEFAULT (datetime('now')),
    ultimo_heartbeat_ts TEXT    NOT NULL DEFAULT (datetime('now')),
    cerrada_ts          TEXT,
    metadatos           TEXT
);


-- ─── locks ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS locks (
    id          INTEGER PRIMARY KEY,
    tarea_id    INTEGER NOT NULL UNIQUE REFERENCES tareas(id)   ON DELETE CASCADE,
    sesion_id   INTEGER NOT NULL        REFERENCES sesiones(id) ON DELETE CASCADE,
    tomado_ts   TEXT    NOT NULL DEFAULT (datetime('now')),
    expira_ts   TEXT    NOT NULL,
    notas       TEXT
);


-- ─── eventos ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS eventos (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL DEFAULT (datetime('now')),
    tipo        TEXT    NOT NULL,
    sesion_id   INTEGER REFERENCES sesiones(id)  ON DELETE SET NULL,
    frente_id   INTEGER REFERENCES frentes(id)   ON DELETE SET NULL,
    tarea_id    INTEGER REFERENCES tareas(id)    ON DELETE SET NULL,
    payload     TEXT,
    mensaje     TEXT
);


-- ─── diario ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS diario (
    id          INTEGER PRIMARY KEY,
    fecha       TEXT    NOT NULL DEFAULT (date('now')),
    ts          TEXT    NOT NULL DEFAULT (datetime('now')),
    frente_id   INTEGER REFERENCES frentes(id) ON DELETE SET NULL,
    autor       TEXT    NOT NULL,
    titulo      TEXT,
    contenido   TEXT    NOT NULL,
    tags        TEXT
);


-- ─── biblioteca ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS biblioteca (
    id              INTEGER PRIMARY KEY,
    slug            TEXT    NOT NULL UNIQUE,
    nombre          TEXT    NOT NULL,
    descripcion     TEXT,
    categoria       TEXT    NOT NULL,
    lenguaje        TEXT,
    estado          TEXT    NOT NULL DEFAULT 'activo'
                    CHECK (estado IN ('activo','legacy','archivado','herramienta')),
    canonical_path  TEXT,
    github          TEXT,
    ultimo_commit   TEXT,
    features        TEXT,
    key_files       TEXT,
    notas           TEXT,
    frente_id       INTEGER REFERENCES frentes(id) ON DELETE SET NULL,
    creado_ts       TEXT    NOT NULL DEFAULT (datetime('now')),
    actualizado_ts  TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- ─── architect_messages ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS architect_messages (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT,
    role        TEXT    NOT NULL CHECK (role IN ('user','architect','system')),
    content     TEXT    NOT NULL,
    metadata    TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- ─── architect_assignments ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS architect_assignments (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER REFERENCES architect_messages(id) ON DELETE CASCADE,
    task_codigo TEXT    NOT NULL,
    model       TEXT,
    hint        TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','rejected','executed')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- ─── skills ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS skills (
    id              INTEGER PRIMARY KEY,
    slug            TEXT    NOT NULL UNIQUE,
    nombre          TEXT    NOT NULL,
    descripcion     TEXT,
    path_skill_md   TEXT    NOT NULL,
    categoria       TEXT,
    tags            TEXT,
    modelo_sugerido TEXT,
    ultima_modif_ts TEXT,
    activa          INTEGER NOT NULL DEFAULT 1
                    CHECK (activa IN (0,1)),
    indexada_ts     TEXT    NOT NULL DEFAULT (datetime('now'))
);


-- ============================================================================
-- ÍNDICES
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_frentes_estado            ON frentes(estado);
CREATE INDEX IF NOT EXISTS idx_objetivos_frente_estado   ON objetivos(frente_id, estado);
CREATE INDEX IF NOT EXISTS idx_objetivos_deadline        ON objetivos(deadline) WHERE deadline IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tareas_frente_estado      ON tareas(frente_id, estado);
CREATE INDEX IF NOT EXISTS idx_tareas_objetivo           ON tareas(objetivo_id);
CREATE INDEX IF NOT EXISTS idx_tareas_estado             ON tareas(estado);
CREATE INDEX IF NOT EXISTS idx_tareas_blocker            ON tareas(blocker_tarea_id) WHERE blocker_tarea_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sesiones_frente_estado    ON sesiones(frente_id, estado);
CREATE INDEX IF NOT EXISTS idx_sesiones_heartbeat        ON sesiones(ultimo_heartbeat_ts) WHERE estado = 'active';
CREATE INDEX IF NOT EXISTS idx_locks_expira              ON locks(expira_ts);
CREATE INDEX IF NOT EXISTS idx_locks_sesion              ON locks(sesion_id);
CREATE INDEX IF NOT EXISTS idx_eventos_ts                ON eventos(ts DESC);
CREATE INDEX IF NOT EXISTS idx_eventos_frente_ts         ON eventos(frente_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_eventos_sesion_ts         ON eventos(sesion_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_eventos_tipo              ON eventos(tipo);
CREATE INDEX IF NOT EXISTS idx_diario_fecha              ON diario(fecha DESC);
CREATE INDEX IF NOT EXISTS idx_diario_frente_fecha       ON diario(frente_id, fecha DESC);
CREATE INDEX IF NOT EXISTS idx_biblioteca_categoria ON biblioteca(categoria);
CREATE INDEX IF NOT EXISTS idx_biblioteca_estado    ON biblioteca(estado);
CREATE INDEX IF NOT EXISTS idx_biblioteca_frente    ON biblioteca(frente_id) WHERE frente_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_biblioteca_github    ON biblioteca(github) WHERE github IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_skills_categoria          ON skills(categoria) WHERE activa = 1;

CREATE INDEX IF NOT EXISTS idx_arch_msgs_created    ON architect_messages(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_arch_msgs_session    ON architect_messages(session_id) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_arch_assign_status   ON architect_assignments(status);
CREATE INDEX IF NOT EXISTS idx_arch_assign_message  ON architect_assignments(message_id);


-- ============================================================================
-- TRIGGERS
-- ============================================================================

CREATE TRIGGER IF NOT EXISTS trg_frentes_actualizado
AFTER UPDATE ON frentes
FOR EACH ROW
BEGIN
    UPDATE frentes SET actualizado_ts = datetime('now') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_objetivos_actualizado
AFTER UPDATE ON objetivos
FOR EACH ROW
BEGIN
    UPDATE objetivos SET actualizado_ts = datetime('now') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_tareas_actualizado
AFTER UPDATE ON tareas
FOR EACH ROW
BEGIN
    UPDATE tareas SET actualizado_ts = datetime('now') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_biblioteca_actualizado
AFTER UPDATE ON biblioteca
FOR EACH ROW
BEGIN
    UPDATE biblioteca SET actualizado_ts = datetime('now') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_objetivos_cerrado
AFTER UPDATE OF estado ON objetivos
FOR EACH ROW
WHEN NEW.estado IN ('done','cancelled') AND OLD.estado NOT IN ('done','cancelled')
BEGIN
    UPDATE objetivos SET cerrado_ts = datetime('now') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_tareas_cerrado
AFTER UPDATE OF estado ON tareas
FOR EACH ROW
WHEN NEW.estado IN ('done','cancelled') AND OLD.estado NOT IN ('done','cancelled')
BEGIN
    UPDATE tareas SET cerrado_ts = datetime('now') WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_lock_insert_estado_tarea
AFTER INSERT ON locks
FOR EACH ROW
BEGIN
    UPDATE tareas
       SET estado = 'locked'
     WHERE id = NEW.tarea_id
       AND estado = 'available';

    INSERT INTO eventos (tipo, sesion_id, tarea_id, frente_id, mensaje)
    SELECT 'lock_acquired', NEW.sesion_id, NEW.tarea_id, t.frente_id,
           'Lock tomado, expira ' || NEW.expira_ts
      FROM tareas t WHERE t.id = NEW.tarea_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_lock_delete_estado_tarea
AFTER DELETE ON locks
FOR EACH ROW
BEGIN
    UPDATE tareas
       SET estado = 'available'
     WHERE id = OLD.tarea_id
       AND estado = 'locked';

    INSERT INTO eventos (tipo, sesion_id, tarea_id, frente_id, mensaje)
    SELECT 'lock_released', OLD.sesion_id, OLD.tarea_id, t.frente_id,
           'Lock liberado'
      FROM tareas t WHERE t.id = OLD.tarea_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_sesion_cerrada_libera_locks
AFTER UPDATE OF estado ON sesiones
FOR EACH ROW
WHEN NEW.estado IN ('closed','crashed') AND OLD.estado NOT IN ('closed','crashed')
BEGIN
    DELETE FROM locks WHERE sesion_id = NEW.id;

    UPDATE sesiones SET cerrada_ts = datetime('now') WHERE id = NEW.id;

    INSERT INTO eventos (tipo, sesion_id, frente_id, mensaje)
    VALUES (
        CASE NEW.estado WHEN 'crashed' THEN 'session_crashed' ELSE 'session_closed' END,
        NEW.id, NEW.frente_id,
        'Session ' || NEW.nombre_tmux || ' closed (' || NEW.estado || ')'
    );
END;


-- ============================================================================
-- VISTAS
-- ============================================================================

CREATE VIEW IF NOT EXISTS v_plan_maestro AS
SELECT
    t.id                AS tarea_id,
    t.codigo,
    t.titulo,
    t.estado            AS tarea_estado,
    t.prioridad,
    f.slug              AS frente_slug,
    f.nombre            AS frente_nombre,
    o.titulo            AS objetivo_titulo,
    s.nombre_tmux       AS lock_owner,
    s.host              AS lock_host,
    l.tomado_ts         AS lock_tomado_ts,
    l.expira_ts         AS lock_expira_ts,
    bl.codigo           AS blocker_codigo,
    bl.estado           AS blocker_estado,
    t.archivos_glob,
    t.creado_ts,
    t.actualizado_ts
FROM tareas t
JOIN frentes f          ON f.id = t.frente_id
LEFT JOIN objetivos o   ON o.id = t.objetivo_id
LEFT JOIN locks l       ON l.tarea_id = t.id
LEFT JOIN sesiones s    ON s.id = l.sesion_id
LEFT JOIN tareas bl     ON bl.id = t.blocker_tarea_id
WHERE t.estado NOT IN ('done','cancelled')
   OR t.cerrado_ts > datetime('now','-7 days');


CREATE VIEW IF NOT EXISTS v_sesiones_activas AS
SELECT
    s.id,
    s.nombre_tmux,
    s.host,
    s.pid,
    s.estado,
    f.slug              AS frente_slug,
    f.nombre            AS frente_nombre,
    s.iniciada_ts,
    s.ultimo_heartbeat_ts,
    CAST((julianday('now') - julianday(s.ultimo_heartbeat_ts)) * 1440 AS INTEGER) AS minutos_desde_heartbeat,
    CASE
        WHEN s.estado != 'active'                                            THEN 'inactive'
        WHEN (julianday('now') - julianday(s.ultimo_heartbeat_ts)) * 1440 < 5  THEN 'healthy'
        WHEN (julianday('now') - julianday(s.ultimo_heartbeat_ts)) * 1440 < 15 THEN 'idle'
        WHEN (julianday('now') - julianday(s.ultimo_heartbeat_ts)) * 1440 < 30 THEN 'stale'
        ELSE 'dead'
    END                 AS salud,
    (SELECT COUNT(*) FROM locks WHERE sesion_id = s.id) AS locks_activos
FROM sesiones s
LEFT JOIN frentes f ON f.id = s.frente_id
WHERE s.estado IN ('active','idle');


CREATE VIEW IF NOT EXISTS v_objetivo_progreso AS
SELECT
    o.id                AS objetivo_id,
    o.titulo,
    o.estado            AS objetivo_estado,
    o.prioridad,
    o.deadline,
    f.slug              AS frente_slug,
    f.nombre            AS frente_nombre,
    COUNT(t.id)         AS total_tareas,
    SUM(CASE WHEN t.estado = 'done'        THEN 1 ELSE 0 END) AS tareas_done,
    SUM(CASE WHEN t.estado = 'in_progress' THEN 1 ELSE 0 END) AS tareas_in_progress,
    SUM(CASE WHEN t.estado = 'blocked'     THEN 1 ELSE 0 END) AS tareas_blocked,
    SUM(CASE WHEN t.estado = 'available'   THEN 1 ELSE 0 END) AS tareas_available,
    CASE WHEN COUNT(t.id) = 0 THEN 0
         ELSE ROUND(100.0 * SUM(CASE WHEN t.estado='done' THEN 1 ELSE 0 END) / COUNT(t.id), 1)
    END                 AS porcentaje_done
FROM objetivos o
JOIN frentes f          ON f.id = o.frente_id
LEFT JOIN tareas t      ON t.objetivo_id = o.id
GROUP BY o.id;


CREATE VIEW IF NOT EXISTS v_locks_proximos_a_expirar AS
SELECT
    l.id                AS lock_id,
    t.codigo            AS tarea_codigo,
    t.titulo            AS tarea_titulo,
    s.nombre_tmux       AS sesion,
    s.host,
    l.tomado_ts,
    l.expira_ts,
    CAST((julianday(l.expira_ts) - julianday('now')) * 1440 AS INTEGER) AS minutos_para_expirar
FROM locks l
JOIN tareas t   ON t.id = l.tarea_id
JOIN sesiones s ON s.id = l.sesion_id
WHERE l.expira_ts <= datetime('now','+5 minutes')
ORDER BY l.expira_ts ASC;
