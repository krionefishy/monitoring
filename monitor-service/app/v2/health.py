import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .storage import Store, health, health_history


def probe(service):
    start = time.monotonic()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(3.0), follow_redirects=False, trust_env=False
        ) as client:
            with client.stream("GET", service.health_url) as response:
                ok = response.status_code == service.expected_status
                reason = None if ok else f"HTTP {response.status_code}"
                if ok and service.health_body is not None:
                    content = b""
                    for part in response.iter_bytes():
                        content += part
                        if len(content) > 65536:
                            raise ValueError("health response too large")
                    import json

                    body = json.loads(content)
                    ok = isinstance(body, dict) and all(
                        body.get(k) == v for k, v in service.health_body.items()
                    )
                    reason = None if ok else "Ответ health не соответствует ожидаемому"
                return ok, (time.monotonic() - start) * 1000, reason
    except (httpx.HTTPError, ValueError):
        return False, None, "Health недоступен или вернул некорректный ответ"


def check_services(store: Store):
    services = [s for s in store.settings.application().services if s.health_url]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, services))
    with store.primary.begin() as conn:
        for service, (ok, elapsed, reason) in zip(services, results):
            old = (
                conn.execute(
                    select(health.c.payload).where(health.c.id == service.id)
                ).scalar_one_or_none()
                or {}
            )
            failures = 0 if ok else old.get("failures", 0) + 1
            state = "healthy" if ok else "degraded" if failures == 1 else "down"
            checked_at = time.time()
            payload = {"latency_ms": elapsed, "reason": reason, "failures": failures}
            conn.execute(
                insert(health)
                .values(id=service.id, checked_at=checked_at, state=state, payload=payload)
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={"checked_at": checked_at, "state": state, "payload": payload},
                )
            )
            conn.execute(
                health_history.insert().values(
                    id=str(uuid.uuid4()),
                    service=service.id,
                    checked_at=checked_at,
                    state=state,
                    latency_ms=elapsed,
                )
            )
