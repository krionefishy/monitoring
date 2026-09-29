import hashlib
import time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .storage import Store, batches, incident_states, incidents, metrics, outbox, route_registry
from .telemetry import Batch, key


def dimensions(row, bucket=True):
    result = dict(
        service=row.service,
        route_group=row.group,
        method=row.method,
        route=row.route,
        status=row.status,
    )
    if bucket:
        result["bucket"] = row.bucket
    return result


def merge(old: dict | None, new: dict) -> dict:
    if old is None:
        return new
    result = dict(new)
    for field in ("count", "latency_sum", "latency_count"):
        result[field] = old.get(field, 0) + new.get(field, 0)
    result["histogram"] = [a + b for a, b in zip(old["histogram"], new["histogram"])]
    result["first_seen"] = min(old["first_seen"], new["first_seen"])
    result["last_seen"] = max(old["last_seen"], new["last_seen"])
    result["samples"] = sorted(
        old.get("samples", []) + new.get("samples", []), key=lambda s: s["time"]
    )[-5:]
    return result


def ingest(store: Store, batch: Batch) -> bool:
    app = store.settings.application()
    if batch.application != app.application or batch.environment != app.environment:
        raise ValueError("Batch belongs to another application/environment")
    digest = hashlib.sha256(batch.model_dump_json().encode()).hexdigest()
    with store.writer() as conn:
        previous = conn.execute(
            select(batches.c.digest).where(batches.c.id == batch.id)
        ).scalar_one_or_none()
        if previous is not None:
            if previous != digest:
                raise ValueError("Batch id reused with different content")
            return False
        cutoff = time.time() - store.settings.retention_days * 86400
        registered = set(conn.execute(select(route_registry.c.id)).scalars())
        new_routes = []
        rows = []
        for original in batch.rows:
            if original.bucket < cutoff:
                continue
            if original.last_seen > time.time() + 300:
                raise ValueError("Future event exceeds five-minute clock tolerance")
            row = original.model_copy()
            route_id = key(row.service, row.group, row.method, row.route)
            if route_id not in registered:
                if len(registered) >= app.max_routes:
                    row.route, row.group = "/[cardinality-limit]", "other"
                else:
                    new_routes.append({"id": route_id})
                    registered.add(route_id)
            rows.append(row)
        if new_routes:
            conn.execute(route_registry.insert(), new_routes)
        changes: dict[str, dict] = {"metrics": {}, "incidents": {}}
        state_updates = {}
        for table, with_bucket in ((metrics, True), (incidents, False)):
            selected = [row for row in rows if with_bucket or row.status >= 400]
            ids = [
                key(app.application, app.environment, *dimensions(row, with_bucket).values())
                for row in selected
            ]
            previous_rows = (
                dict(
                    conn.execute(
                        select(table.c.id, table.c.payload).where(table.c.id.in_(ids))
                    ).all()
                )
                if ids
                else {}
            )
            states = (
                {
                    r["id"]: dict(r)
                    for r in conn.execute(
                        select(incident_states).where(incident_states.c.id.in_(ids))
                    ).mappings()
                }
                if ids and not with_bucket
                else {}
            )
            for row, row_id in zip(selected, ids):
                data = row.model_dump(
                    exclude={"bucket", "service", "group", "method", "route", "status"}
                )
                payload = merge(previous_rows.get(row_id), data)
                previous_rows[row_id] = payload
                changes[table.name][row_id] = dict(
                    id=row_id, **dimensions(row, with_bucket), payload=payload
                )
                if not with_bucket:
                    state = states.get(row_id)
                    if state is None:
                        state = dict(
                            id=row_id,
                            state="open",
                            resolved_through=0,
                            updated=time.time(),
                            updated_by="collector",
                            version=1,
                        )
                        states[row_id] = state
                        state_updates[row_id] = state
                    elif state["state"] == "resolved" and row.bucket >= state["resolved_through"]:
                        state.update(
                            state="open",
                            updated=time.time(),
                            updated_by="collector",
                            version=state["version"] + 1,
                        )
                        state_updates[row_id] = state
            if changes[table.name]:
                statement = insert(table)
                conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=["id"], set_={"payload": statement.excluded.payload}
                    ),
                    list(changes[table.name].values()),
                )
        if state_updates:
            statement = insert(incident_states)
            conn.execute(
                statement.on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        name: getattr(statement.excluded, name)
                        for name in (
                            "state",
                            "resolved_through",
                            "updated",
                            "updated_by",
                            "version",
                        )
                    },
                ),
                list(state_updates.values()),
            )
        conn.execute(batches.insert().values(id=batch.id, digest=digest, created=time.time()))
        conn.execute(
            outbox.insert().values(
                id=batch.id,
                created=time.time(),
                payload={name: list(items.values()) for name, items in changes.items()},
            )
        )
    return True
