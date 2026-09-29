import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .auth import (
    admin_user,
    cookie_name,
    current_user,
    login,
    mutation_user,
    token_hash,
)
from .config import Settings
from .queries import analytics, diagnostics, filters, service_status
from .storage import Store, incident_states, incidents, metrics, publication, sessions

log = logging.getLogger(__name__)


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class StateChange(BaseModel):
    state: Literal["open", "acknowledged", "resolved"]
    version: int = Field(ge=1)


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    config = settings.application()
    store = Store(settings)
    cache = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
        decode_responses=True,
    )

    @asynccontextmanager
    async def lifespan(app):
        store.validate_instance()
        yield
        store.close()
        cache.close()

    app = FastAPI(
        title="Monitoring",
        version="2.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings, app.state.store, app.state.cache = settings, store, cache

    @app.middleware("http")
    async def headers(request, call_next):
        if (
            request.headers.get("content-length", "0").isdigit()
            and int(request.headers.get("content-length", "0")) > 8192
        ):
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(SQLAlchemyError)
    async def storage_unavailable(request, exc):
        log.error("Database unavailable: %s", type(exc).__name__)
        return JSONResponse(
            {
                "detail": "Хранилище временно недоступно. Данные будут восстановлены после подключения."
            },
            status_code=503,
        )

    @app.get("/healthz")
    def liveness():
        return {"status": "ok"}

    @app.post("/api/errors")
    def retired():
        raise HTTPException(
            410,
            "SDK ingest retired. Configure the nginx JSON access log for Monitoring v2.",
        )

    @app.post("/api/auth/login")
    def sign_in(body: Credentials, request: Request, response: Response):
        token, user = login(request, body.username, body.password)
        response.set_cookie(
            cookie_name(settings),
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=settings.session_hours * 3600,
            path="/",
        )
        return user

    @app.get("/api/auth/me")
    def me(user=Depends(current_user)):
        return {k: user[k] for k in ("username", "role", "csrf")}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, user=Depends(mutation_user)):
        with store.primary.begin() as conn:
            conn.execute(
                sessions.delete().where(
                    sessions.c.token_hash
                    == token_hash(request, request.cookies.get(cookie_name(settings), ""))
                )
            )
        response.delete_cookie(
            cookie_name(settings),
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return {"ok": True}

    @app.get("/api/config")
    def public_config(user=Depends(current_user)):
        with store.read.connect() as conn:
            groups = list(
                conn.execute(
                    select(metrics.c.route_group).distinct().order_by(metrics.c.route_group)
                ).scalars()
            )
        return dict(
            instance=settings.instance_id,
            application=config.application,
            environment=config.environment,
            topology=config.topology,
            services=[{"id": s.id, "name": s.name} for s in config.services],
            groups=groups,
            retention_days=settings.retention_days,
            publish_seconds=settings.publish_seconds,
        )

    @app.get("/api/metrics")
    def metric_query(
        hours: int = Query(24, ge=1, le=720),
        service: str = "",
        group: str = "",
        route: str = "",
        method: str = "",
        end: int | None = None,
        user=Depends(current_user),
    ):
        hours = min(hours, settings.retention_days * 24)
        end = (end or int(time.time())) // 60 * 60
        if end > time.time() + 60 or end < time.time() - settings.retention_days * 86400:
            raise HTTPException(422, "Период вне срока хранения данных")
        start = end - hours * 3600
        step = 60 if hours <= 6 else 300 if hours <= 24 else 1800 if hours <= 168 else 7200
        # Publication and all metric reads share one PostgreSQL MVCC snapshot.
        with (
            store.read.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            snapshot = (
                conn.execute(select(publication).where(publication.c.id == 1)).mappings().one()
            )
            params = [hours, start, end, step, service, group, route, method]
            digest = hashlib.sha256(json.dumps(params).encode()).hexdigest()
            cache_key = f"metrics:{settings.instance_id}:{snapshot['generation']}:{digest}"
            try:
                saved = cache.get(cache_key)
            except RedisError:
                saved = None
            if saved:
                result = json.loads(saved)
            else:
                result = analytics(conn, start, end, step, service, group, route, method)
                result.update(
                    generation=snapshot["generation"],
                    published_at=snapshot["published_at"],
                    newest_event=snapshot["newest_event"],
                )
                try:
                    cache.setex(cache_key, 30, json.dumps(result))
                except RedisError:
                    pass
        result["workers"] = diagnostics(store)
        return result

    @app.get("/api/incidents")
    def incident_list(
        service: str = "",
        group: str = "",
        route: str = "",
        method: str = "",
        state: str = "",
        offset: int = Query(0, ge=0),
        user=Depends(current_user),
    ):
        with store.read.connect() as conn:
            # Read-model counts, current primary states: acknowledge/resolve has immediate feedback.
            rows = (
                conn.execute(
                    select(incidents)
                    .where(*filters(incidents, service, group, route, method))
                    .order_by(incidents.c.payload["last_seen"].as_float().desc())
                )
                .mappings()
                .all()
            )
        with store.primary.connect() as conn:
            states = {s["id"]: dict(s) for s in conn.execute(select(incident_states)).mappings()}
        items = []
        for row in rows:
            status = states.get(row["id"], {"state": "open", "version": 1})
            if not state or status["state"] == state:
                items.append({**row, "lifecycle": status})
        return {
            "items": items[offset : offset + 100],
            "total": len(items),
            "offset": offset,
        }

    @app.get("/api/incidents/{incident_id}")
    def incident_detail(incident_id: str, user=Depends(current_user)):
        with store.read.connect() as conn:
            row = (
                conn.execute(select(incidents).where(incidents.c.id == incident_id))
                .mappings()
                .first()
            )
        if row is None:
            raise HTTPException(404, "Инцидент не найден")
        with store.primary.connect() as conn:
            state = (
                conn.execute(select(incident_states).where(incident_states.c.id == incident_id))
                .mappings()
                .one()
            )
        return {**row, "lifecycle": dict(state)}

    @app.patch("/api/incidents/{incident_id}")
    def change_state(incident_id: str, body: StateChange, user=Depends(admin_user)):
        with store.writer() as conn:
            old = (
                conn.execute(select(incident_states).where(incident_states.c.id == incident_id))
                .mappings()
                .first()
            )
            if old is None:
                raise HTTPException(404, "Инцидент не найден")
            if old["version"] != body.version:
                raise HTTPException(409, "Статус уже изменился. Обновите страницу.")
            # Resolution covers the current 30-second bucket, including delayed arrivals.
            cutoff = (
                (int(time.time()) // 30 + 1) * 30
                if body.state == "resolved"
                else old["resolved_through"]
            )
            values = dict(
                state=body.state,
                resolved_through=cutoff,
                updated=time.time(),
                updated_by=user["username"],
                version=old["version"] + 1,
            )
            conn.execute(
                incident_states.update().where(incident_states.c.id == incident_id).values(**values)
            )
        return dict(id=incident_id, **values)

    @app.get("/api/services-status")
    def statuses(user=Depends(current_user)):
        return {"services": service_status(store), "workers": diagnostics(store)}

    assets = settings.frontend_path / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith(("api/", "assets/")):
            raise HTTPException(404)
        index = settings.frontend_path / "index.html"
        if not index.exists():
            raise HTTPException(503, "Frontend build is not installed")
        return FileResponse(index)

    return app
