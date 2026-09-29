import hashlib
import time
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from .storage import Store, metrics, incidents, incident_states, batches, outbox
from .telemetry import Batch, key


def dimensions(row, bucket=True):
    result = dict(service=row.service, route_group=row.group, method=row.method, route=row.route, status=row.status)
    if bucket:
        result['bucket'] = row.bucket
    return result


def merge(old: dict | None, new: dict) -> dict:
    if old is None:
        return new
    result = dict(new)
    for field in ('count', 'latency_sum', 'latency_count'):
        result[field] = old.get(field, 0) + new.get(field, 0)
    result['histogram'] = [a + b for a, b in zip(old['histogram'], new['histogram'])]
    result['first_seen'] = min(old['first_seen'], new['first_seen'])
    result['last_seen'] = max(old['last_seen'], new['last_seen'])
    result['samples'] = sorted(old.get('samples', []) + new.get('samples', []), key=lambda s: s['time'])[-5:]
    return result


def ingest(store: Store, batch: Batch) -> bool:
    app = store.settings.application()
    if batch.application != app.application or batch.environment != app.environment:
        raise ValueError('Batch belongs to another application/environment')
    digest = hashlib.sha256(batch.model_dump_json().encode()).hexdigest()
    with store.writer() as conn:
        previous = conn.execute(select(batches.c.digest).where(batches.c.id == batch.id)).scalar_one_or_none()
        if previous is not None:
            if previous != digest:
                raise ValueError('Batch id reused with different content')
            return False
        changes: dict[str, dict] = {'metrics': {}, 'incidents': {}}
        for row in batch.rows:
            if row.bucket < time.time() - store.settings.retention_days * 86400:
                continue
            if row.last_seen > time.time() + 300:
                raise ValueError('Future event exceeds five-minute clock tolerance')
            data = row.model_dump(exclude={'bucket', 'service', 'group', 'method', 'route', 'status'})
            for table, with_bucket in ((metrics, True), (incidents, False)):
                if not with_bucket and row.status < 400:
                    continue
                dims = dimensions(row, with_bucket)
                row_id = key(app.application, app.environment, *dims.values())
                old = conn.execute(select(table.c.payload).where(table.c.id == row_id)).scalar_one_or_none()
                payload = merge(old, data)
                values = dict(id=row_id, **dims, payload=payload)
                conn.execute(insert(table).values(**values).on_conflict_do_update(index_elements=['id'], set_={'payload': payload}))
                changes[table.name][row_id] = values
                if not with_bucket:
                    state = conn.execute(select(incident_states).where(incident_states.c.id == row_id)).mappings().first()
                    if state is None:
                        conn.execute(incident_states.insert().values(id=row_id, state='open', resolved_through=0, updated=time.time(), updated_by='collector', version=1))
                    elif state['state'] == 'resolved' and row.bucket >= state['resolved_through']:
                        conn.execute(incident_states.update().where(incident_states.c.id == row_id).values(state='open', updated=time.time(), updated_by='collector', version=state['version'] + 1))
        conn.execute(batches.insert().values(id=batch.id, digest=digest, created=time.time()))
        conn.execute(outbox.insert().values(id=batch.id, created=time.time(), payload={name: list(rows.values()) for name, rows in changes.items()}))
    return True
