import hashlib
import hmac
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from redis.exceptions import RedisError
from sqlalchemy import select

from .storage import sessions, users

passwords = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(32))


def token_hash(request: Request, token: str) -> str:
    settings = request.app.state.settings
    return hmac.new(
        settings.secret_key.get_secret_value().encode(),
        f"{settings.instance_id}:{token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def cookie_name(settings):
    return "__Host-monitor_session" if settings.cookie_secure else "monitor_session"


def current_user(request: Request):
    token = request.cookies.get(cookie_name(request.app.state.settings), "")
    with request.app.state.store.primary.connect() as conn:
        row = (
            conn.execute(
                select(users.c.id, users.c.username, users.c.role, sessions.c.csrf)
                .join(sessions, users.c.id == sessions.c.user_id)
                .where(
                    sessions.c.token_hash == token_hash(request, token),
                    sessions.c.instance_id == request.app.state.settings.instance_id,
                    sessions.c.expires > time.time(),
                    users.c.enabled.is_(True),
                )
            )
            .mappings()
            .first()
        )
    if not row:
        raise HTTPException(401, "Требуется вход")
    return dict(row)


def mutation_user(request: Request):
    user = current_user(request)
    if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), user["csrf"]):
        raise HTTPException(403, "Недействительный CSRF-токен")
    return user


def admin_user(request: Request):
    user = mutation_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Недостаточно прав")
    return user


def login(request: Request, username: str, password: str):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != (
        request.app.state.settings.public_origin or str(request.base_url)
    ).rstrip("/"):
        raise HTTPException(403, "Недопустимый источник запроса")
    client = request.client.host if request.client else "unknown"
    # HMAC prevents raw usernames/IP addresses from being stored in Redis keys.
    identity = token_hash(request, username.casefold())
    ip_key = token_hash(request, client)
    try:
        for scope in (identity, ip_key):
            attempts = request.app.state.cache.eval(
                "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],300) end; return n",
                1,
                f"login:{request.app.state.settings.instance_id}:{scope}",
            )
            if attempts > 10:
                raise HTTPException(429, "Слишком много попыток. Повторите через 5 минут.")
    except RedisError:
        raise HTTPException(503, "Вход временно недоступен") from None
    store = request.app.state.store
    with store.primary.begin() as conn:
        user = (
            conn.execute(select(users).where(users.c.username == username).with_for_update())
            .mappings()
            .first()
        )
        try:
            valid = passwords.verify(user["password_hash"] if user else DUMMY_HASH, password)
        except (VerificationError, InvalidHashError):
            valid = False
        if not valid or not user or not user["enabled"]:
            raise HTTPException(401, "Неверный логин или пароль")
        token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        conn.execute(
            sessions.insert().values(
                token_hash=token_hash(request, token),
                user_id=user["id"],
                instance_id=store.settings.instance_id,
                csrf=csrf,
                expires=time.time() + store.settings.session_hours * 3600,
            )
        )
    return token, dict(username=user["username"], role=user["role"], csrf=csrf)
