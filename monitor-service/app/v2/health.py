import asyncio
import json
import time
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .storage import Store, health, health_history

PROBE_TIMEOUT = 3.0


async def probe(service):
    start = time.monotonic()
    try:
        # Bound the whole exchange, including a body delivered in tiny, slow chunks.
        async with asyncio.timeout(PROBE_TIMEOUT):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(PROBE_TIMEOUT), follow_redirects=False, trust_env=False
            ) as client:
                async with client.stream("GET", service.health_url) as response:
                    ok = response.status_code == service.expected_status
                    reason = None if ok else f"HTTP {response.status_code}"
                    if ok and service.health_body is not None:
                        content = bytearray()
                        async for part in response.aiter_bytes():
                            content.extend(part)
                            if len(content) > 65536:
                                raise ValueError("health response too large")
                        body = json.loads(content)
                        ok = isinstance(body, dict) and all(
                            body.get(k) == v for k, v in service.health_body.items()
                        )
                        reason = None if ok else "Ответ health не соответствует ожидаемому"
                    return ok, (time.monotonic() - start) * 1000, reason
    except TimeoutError:
        return False, None, "Превышено время проверки health"
    except (httpx.HTTPError, ValueError):
        return False, None, "Health недоступен или вернул некорректный ответ"


def save_result(store, service, result):
    ok, elapsed, reason = result
    with store.primary.begin() as conn:
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


async def check_services_async(store):
    semaphore = asyncio.Semaphore(8)

    async def check(service):
        async with semaphore:
            result = await probe(service)
            # DB writes must not block other probes' deadlines on the event loop.
            await asyncio.to_thread(save_result, store, service, result)

    async with asyncio.TaskGroup() as tasks:
        for service in store.settings.application().services:
            if service.health_url:
                tasks.create_task(check(service))


def check_services(store: Store):
    asyncio.run(check_services_async(store))
