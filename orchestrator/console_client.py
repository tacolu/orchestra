"""Async client for the Orchestra Console API."""

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("orchestrator.console")


class ConsoleAPIError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


class ConsoleClient:
    def __init__(self, base_url: str):
        self._base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._base, timeout=15.0)

    async def close(self):
        await self._http.aclose()

    # -- helpers --

    async def _req(self, method: str, path: str, **kwargs) -> Any:
        """Request with retry (3 attempts, exponential backoff)."""
        last_err = None
        for attempt in range(3):
            try:
                resp = await self._http.request(method, path, **kwargs)
                if resp.status_code == 204:
                    return None
                if resp.status_code >= 400:
                    detail = resp.text
                    try:
                        detail = resp.json().get("detail", detail)
                        if isinstance(detail, dict):
                            detail = f"{detail.get('error', '')}: {detail.get('detail', '')}"
                    except Exception:
                        pass
                    raise ConsoleAPIError(resp.status_code, str(detail))
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    return resp.json()
                return resp.text
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
                last_err = e
                wait = 2 ** attempt
                log.warning("API %s %s attempt %d failed: %s (retry in %ds)", method, path, attempt + 1, e, wait)
                await asyncio.sleep(wait)
        raise ConnectionError(f"API unreachable after 3 attempts: {last_err}")

    async def get(self, path: str, **params) -> Any:
        return await self._req("GET", path, params=params)

    async def post(self, path: str, body: dict | None = None) -> Any:
        return await self._req("POST", path, json=body)

    async def patch(self, path: str, body: dict) -> Any:
        return await self._req("PATCH", path, json=body)

    async def delete(self, path: str, **params) -> Any:
        return await self._req("DELETE", path, params=params)

    # -- sessions --

    async def register_session(self, nombre: str, frente: str, host: str, pid: int) -> dict:
        return await self.post("/v1/sesiones", {
            "nombre_tmux": nombre,
            "frente": frente,
            "host": host,
            "pid": pid,
            "metadatos": {"tipo": "orchestrator"},
        })

    async def heartbeat(self, sid: int, ttl_min: int = 30) -> dict:
        return await self.post(f"/v1/sesiones/{sid}/heartbeat", {"ttl_min": ttl_min})

    async def close_session(self, sid: int, motivo: str, nota_diario: str | None = None) -> None:
        await self.post(f"/v1/sesiones/{sid}/cerrar", {
            "motivo": motivo,
            "nota_diario": nota_diario,
        })

    # -- tasks --

    async def get_available_tasks(self, frente: str, limit: int = 10) -> list[dict]:
        return await self.get("/v1/sesion/disponibles", frente=frente, limit=limit)

    async def get_tasks_by_estado(self, frente: str, estado: str) -> list[dict]:
        return await self.get("/v1/tareas", frente=frente, estado=estado)

    async def get_task(self, codigo: str) -> dict:
        return await self.get(f"/v1/tareas/{codigo}")

    async def patch_task(self, codigo: str, **fields) -> dict:
        return await self.patch(f"/v1/tareas/{codigo}", fields)

    async def create_task(self, frente: str, codigo: str, titulo: str, **fields) -> dict:
        body = {"frente": frente, "codigo": codigo, "titulo": titulo}
        body.update(fields)
        return await self.post("/v1/tareas", body)

    async def delete_task(self, codigo: str) -> None:
        await self.delete(f"/v1/tareas/{codigo}")

    # -- locks --

    async def acquire_lock(self, tarea_codigo: str, sesion_id: int, ttl_min: int = 30) -> dict:
        return await self.post("/v1/locks", {
            "tarea_codigo": tarea_codigo,
            "sesion_id": sesion_id,
            "ttl_min": ttl_min,
        })

    async def release_lock(self, lock_id: int, sesion_id: int) -> None:
        await self.delete(f"/v1/locks/{lock_id}", sesion_id=sesion_id)

    async def renew_lock(self, lock_id: int, sesion_id: int, ttl_min: int = 30) -> dict:
        return await self.post(f"/v1/locks/{lock_id}/renovar", {
            "sesion_id": sesion_id,
            "ttl_min": ttl_min,
        })

    # -- plan / context --

    async def get_plan(self) -> dict:
        return await self.get("/v1/plan")

    async def get_plan_snapshot(self) -> str:
        return await self.get("/v1/plan/snapshot")

    async def get_frente(self, slug: str) -> dict:
        return await self.get(f"/v1/frentes/{slug}")

    async def get_stats(self) -> dict:
        return await self.get("/v1/stats")

    # -- skills --

    async def list_skills(self) -> list[dict]:
        return await self.get("/v1/skills")

    async def get_skill_content(self, slug: str) -> str:
        return await self.get(f"/v1/skills/{slug}/contenido")

    # -- observability --

    async def emit_event(self, mensaje: str, tarea_codigo: str | None = None,
                         frente: str | None = None, sesion_id: int | None = None,
                         payload: dict | None = None) -> dict:
        body: dict = {"tipo": "note", "mensaje": mensaje}
        if tarea_codigo:
            body["tarea_codigo"] = tarea_codigo
        if frente:
            body["frente"] = frente
        if sesion_id:
            body["sesion_id"] = sesion_id
        if payload:
            body["payload"] = payload
        return await self.post("/v1/eventos", body)

    async def log_diario(self, autor: str, contenido: str, frente: str | None = None,
                         titulo: str | None = None, tags: str | None = None) -> dict:
        body: dict = {"autor": autor, "contenido": contenido}
        if frente:
            body["frente"] = frente
        if titulo:
            body["titulo"] = titulo
        if tags:
            body["tags"] = tags
        return await self.post("/v1/diario", body)

    # -- active locks --

    async def list_locks(self, sesion_id: int | None = None) -> list[dict]:
        params = {}
        if sesion_id:
            params["sesion_id"] = sesion_id
        return await self.get("/v1/locks", **params)

    # ── Architect Chat ──────────────────────────────────────────────────

    async def get_architect_messages(self, since: str | None = None, limit: int = 50) -> list[dict]:
        params = {"limit": limit}
        if since:
            params["since"] = since
        return await self.get("/v1/architect/messages", **params)

    async def post_architect_message(self, content: str, role: str = "architect",
                                     session_id: str | None = None,
                                     metadata: dict | None = None) -> dict:
        body = {"role": role, "content": content, "session_id": session_id}
        if metadata:
            body["metadata"] = metadata
        return await self.post("/v1/architect/messages", body)

    async def post_architect_assignments(self, message_id: int, assignments: list[dict]) -> list[dict]:
        """Post assignment records linked to an architect message."""
        results = []
        for a in assignments:
            body = {
                "message_id": message_id,
                "task_codigo": a["tarea_codigo"],
                "model": a.get("model"),
                "hint": a.get("worker_hint"),
            }
            r = await self.post("/v1/architect/assignments", body)
            results.append(r)
        return results

    async def get_pending_assignments(self) -> list[dict]:
        return await self.get("/v1/architect/assignments", status="pending")

    async def get_architect_config(self) -> dict:
        return await self.get("/v1/architect/config")
