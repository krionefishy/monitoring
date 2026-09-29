import time
import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from .storage import (
    Store,
    batches,
    health_history,
    incident_states,
    incidents,
    metrics,
    outbox,
    publication,
    receipts,
    sessions,
)


def publish(store: Store, limit: int = 100) -> int:
    # The primary transaction is held through read commit and acknowledgement.
    # A crash after read commit replays receipts rather than increments counters.
    with store.writer() as source:
        pending = (
            source.execute(select(outbox).order_by(outbox.c.sequence).limit(limit)).mappings().all()
        )
        with store.read.begin() as target:
            store.validate_read_instance(target)
            target.execute(text("SELECT pg_advisory_xact_lock(7262402)"))
            state = (
                target.execute(select(publication).where(publication.c.id == 1)).mappings().one()
            )
            newest = state["newest_event"]
            for batch in pending:
                applied = target.execute(
                    select(receipts.c.id).where(receipts.c.id == batch["id"])
                ).scalar_one_or_none()
                if not applied:
                    for table in (metrics, incidents):
                        records = batch["payload"][table.name]
                        if records:
                            statement = insert(table)
                            target.execute(
                                statement.on_conflict_do_update(
                                    index_elements=["id"],
                                    set_={"payload": statement.excluded.payload},
                                ),
                                records,
                            )
                            newest = max(
                                newest, max(row["payload"]["last_seen"] for row in records)
                            )
                    target.execute(receipts.insert().values(id=batch["id"], created=time.time()))
            cutoff = time.time() - store.settings.retention_days * 86400
            target.execute(receipts.delete().where(receipts.c.created < cutoff - 86400))
            target.execute(metrics.delete().where(metrics.c.bucket < cutoff))
            target.execute(
                incidents.delete().where(incidents.c.payload["last_seen"].as_float() < cutoff)
            )
            target.execute(
                publication.update()
                .where(publication.c.id == 1)
                .values(
                    generation=str(uuid.uuid4()),
                    published_at=time.time(),
                    newest_event=newest,
                )
            )
        if pending:
            source.execute(outbox.delete().where(outbox.c.id.in_([row["id"] for row in pending])))
        source.execute(metrics.delete().where(metrics.c.bucket < cutoff))
        source.execute(
            incidents.delete().where(incidents.c.payload["last_seen"].as_float() < cutoff)
        )
        source.execute(
            incident_states.delete().where(~incident_states.c.id.in_(select(incidents.c.id)))
        )
        source.execute(
            batches.delete().where(
                batches.c.created < cutoff - 86400, ~batches.c.id.in_(select(outbox.c.id))
            )
        )
        source.execute(health_history.delete().where(health_history.c.checked_at < cutoff))
        source.execute(sessions.delete().where(sessions.c.expires < time.time()))
    return len(pending)
