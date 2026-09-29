import json
import time
import uuid

import pytest
from app.v2.api import create_app
from app.v2.auth import passwords
from app.v2.ingest import ingest
from app.v2.projection import publish
from app.v2.storage import incident_states, incidents, metrics, outbox, users
from app.v2.telemetry import Batch, aggregate, parse_record
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import func, select
from test_collector import line


def make_batch(store, codes=(200, 404, 409, 500), timestamp=None):
    config = store.settings.application()
    timestamp = timestamp or (int(time.time()) // 60 * 60 - 120)
    return Batch(
        id=str(uuid.uuid4()),
        source="test",
        config_hash=config.fingerprint,
        application="test",
        environment="test",
        rows=aggregate([parse_record(line(code, timestamp=timestamp), config) for code in codes]),
    )


def test_ingest_retry_publication_and_replay(store):
    batch = make_batch(store)
    assert ingest(store, batch)
    assert not ingest(store, batch)
    with store.primary.connect() as conn:
        assert conn.execute(select(func.count()).select_from(incidents)).scalar() == 3
        saved = dict(conn.execute(select(outbox)).mappings().one())
    assert publish(store) == 1
    saved.pop("sequence", None)
    with store.read.connect() as conn:
        assert sum(r["count"] for r in conn.execute(select(metrics.c.payload)).scalars()) == 4
    # Simulate a crash after target commit but before source acknowledgement.
    with store.primary.begin() as conn:
        conn.execute(outbox.insert().values(**saved))
    assert publish(store) == 1
    with store.read.connect() as conn:
        assert sum(r["count"] for r in conn.execute(select(metrics.c.payload)).scalars()) == 4
    corrupt = batch.model_copy(deep=True)
    corrupt.rows[0].count = 999
    with pytest.raises(ValueError):
        ingest(store, corrupt)


def test_auth_csrf_isolation_and_metrics(store):
    settings = store.settings
    cache = Redis.from_url(settings.redis_url)
    cache.flushdb()
    with store.primary.begin() as conn:
        conn.execute(
            users.insert().values(
                id="u",
                username="admin",
                password_hash=passwords.hash("correct-password"),
                role="admin",
                enabled=True,
            )
        )
    ingest(store, make_batch(store))
    publish(store)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/metrics").status_code == 401
        assert client.post("/api/errors", json={}).status_code == 410
        assert (
            client.post(
                "/api/auth/login", json={"username": "admin", "password": "wrong"}
            ).status_code
            == 401
        )
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "correct-password"},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf"]
        result = client.get("/api/metrics?hours=1").json()
        assert result["total"] == 4
        assert result["codes"] == {"200": 1, "404": 1, "409": 1, "500": 1}
        incident = client.get("/api/incidents").json()["items"][0]
        assert (
            client.patch(
                "/api/incidents/" + incident["id"],
                json={"state": "resolved", "version": 1},
            ).status_code
            == 403
        )
        assert (
            client.patch(
                "/api/incidents/" + incident["id"],
                json={"state": "resolved", "version": 1},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 200
        )
        assert (
            client.get("/api/incidents/" + incident["id"]).json()["lifecycle"]["state"]
            == "resolved"
        )
        assert (
            client.patch(
                "/api/incidents/" + incident["id"],
                json={"state": "open", "version": 1},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 409
        )
        # Even if the same physical DB is accidentally addressed, instance-bound sessions reject it.
        client.app.state.settings.instance_id = "other-instance"
        assert client.get("/api/auth/me").status_code == 401
        client.app.state.settings.instance_id = "test-instance"
        assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert client.get("/api/auth/me").status_code == 401


def test_late_data_does_not_reopen_but_new_bucket_does(store):
    timestamp = int(time.time()) // 30 * 30 - 90
    ingest(store, make_batch(store, (404,), timestamp))
    with store.primary.begin() as conn:
        conn.execute(
            incident_states.update().values(state="resolved", resolved_through=timestamp + 30)
        )
    ingest(store, make_batch(store, (404,), timestamp + 1))
    with store.primary.connect() as conn:
        assert conn.execute(select(incident_states.c.state)).scalar() == "resolved"
    ingest(store, make_batch(store, (404,), timestamp + 31))
    with store.primary.connect() as conn:
        assert conn.execute(select(incident_states.c.state)).scalar() == "open"


def test_generation_invalidates_cache_and_redis_failure_is_readable(store, monkeypatch):
    Redis.from_url(store.settings.redis_url).flushdb()
    with store.primary.begin() as conn:
        conn.execute(
            users.insert().values(
                id="u",
                username="admin",
                password_hash=passwords.hash("correct-password"),
                role="admin",
                enabled=True,
            )
        )
    ingest(store, make_batch(store, (200,)))
    publish(store)
    with TestClient(create_app(store.settings)) as client:
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "correct-password"},
        )
        first = client.get("/api/metrics?hours=1").json()
        assert first["total"] == 1
        ingest(store, make_batch(store, (404,)))
        publish(store)
        second = client.get("/api/metrics?hours=1").json()
        assert second["total"] == 2
        assert first["generation"] != second["generation"]
        from redis.exceptions import ConnectionError

        def unavailable(*args, **kwargs):
            raise ConnectionError("offline")

        monkeypatch.setattr(client.app.state.cache, "get", unavailable)
        monkeypatch.setattr(client.app.state.cache, "setex", unavailable)
        assert client.get("/api/metrics?hours=1").json()["total"] == 2


def test_projection_keeps_commit_order_when_clocks_move_back(store):
    first = make_batch(store, (200,))
    second = make_batch(store, (200, 200))
    ingest(store, first)
    ingest(store, second)
    with store.primary.begin() as conn:
        conn.execute(
            outbox.update().where(outbox.c.id == first.id).values(created=time.time() + 1000)
        )
    publish(store)
    with store.read.connect() as conn:
        assert sum(p["count"] for p in conn.execute(select(metrics.c.payload)).scalars()) == 3


def test_health_transitions_and_staleness(store, monkeypatch):
    from app.v2 import health as service_health
    from app.v2.queries import service_status
    from app.v2.storage import health

    config = json.loads(store.settings.config_path.read_text())
    config["services"] = [{"id": "api", "name": "API", "health_url": "http://localhost/health"}]
    store.settings.config_path.write_text(json.dumps(config))
    monkeypatch.setattr(service_health, "probe", lambda _: (False, None, "unavailable"))
    service_health.check_services(store)
    assert service_status(store)[0]["state"] == "degraded"
    service_health.check_services(store)
    assert service_status(store)[0]["state"] == "down"
    monkeypatch.setattr(service_health, "probe", lambda _: (True, 20, None))
    service_health.check_services(store)
    assert service_status(store)[0]["state"] == "healthy"
    with store.primary.begin() as conn:
        conn.execute(health.update().values(checked_at=time.time() - 100))
    assert service_status(store)[0]["state"] == "unknown"


def test_read_database_of_another_instance_is_never_published(store):
    from app.v2.storage import instances
    from sqlalchemy.exc import SQLAlchemyError

    ingest(store, make_batch(store))
    with store.read.begin() as conn:
        conn.execute(instances.update().values(id="foreign-instance"))
    with pytest.raises(SQLAlchemyError):
        publish(store)
    with store.primary.connect() as conn:
        assert conn.execute(select(func.count()).select_from(outbox)).scalar() == 1


def test_collector_to_database_and_route_cardinality_limit(store, tmp_path):
    from app.v2.collector import Collector

    config = store.settings.application()
    config.max_routes = 10
    store.settings.config_path.write_text(config.model_dump_json())
    path = tmp_path / "access.json"
    timestamp = int(time.time()) // 60 * 60 - 120
    path.write_bytes(
        b"".join(line(404, path=f"/home/users/{i}", timestamp=timestamp) for i in range(20))
    )
    collector = Collector(config, tmp_path / "spool")
    collector.scan()
    for batch_id, batch in collector.pending():
        ingest(store, batch)
        collector.acknowledge(batch_id)
    collector.close()
    publish(store)
    with store.read.connect() as conn:
        rows = conn.execute(select(metrics)).mappings().all()
        assert sum(r["payload"]["count"] for r in rows) == 20
        assert len(rows) == 11
        assert any(
            r["route"] == "/[cardinality-limit]" and r["payload"]["count"] == 10 for r in rows
        )
