from contextlib import contextmanager
import hashlib
import time

from sqlalchemy import (Boolean, Column, Float, Integer, BigInteger, MetaData, String,
                        Table, Text, create_engine, select, text)
from sqlalchemy.dialects.postgresql import JSONB
from .config import Settings

metadata = MetaData()
instances = Table('instances', metadata, Column('id', String, primary_key=True), Column('secret_fingerprint', String, nullable=False))
users = Table('users', metadata, Column('id', String, primary_key=True), Column('username', String, unique=True, nullable=False), Column('password_hash', Text, nullable=False), Column('role', String, nullable=False), Column('enabled', Boolean, nullable=False, default=True))
sessions = Table('sessions', metadata, Column('token_hash', String, primary_key=True), Column('user_id', String, nullable=False), Column('instance_id', String, nullable=False), Column('csrf', String, nullable=False), Column('expires', Float, nullable=False))
metrics = Table('metrics', metadata, Column('id', String, primary_key=True), Column('bucket', BigInteger, nullable=False, index=True), Column('service', String, nullable=False, index=True), Column('route_group', String, nullable=False), Column('method', String, nullable=False), Column('route', Text, nullable=False), Column('status', Integer, nullable=False), Column('payload', JSONB, nullable=False))
incidents = Table('incidents', metadata, Column('id', String, primary_key=True), Column('service', String, nullable=False), Column('route_group', String, nullable=False), Column('method', String, nullable=False), Column('route', Text, nullable=False), Column('status', Integer, nullable=False), Column('payload', JSONB, nullable=False))
incident_states = Table('incident_states', metadata, Column('id', String, primary_key=True), Column('state', String, nullable=False), Column('resolved_through', BigInteger, nullable=False, default=0), Column('updated', Float, nullable=False), Column('updated_by', String, nullable=False), Column('version', Integer, nullable=False, default=1))
batches = Table('batches', metadata, Column('id', String, primary_key=True), Column('digest', String, nullable=False), Column('created', Float, nullable=False))
outbox = Table('outbox', metadata, Column('id', String, primary_key=True), Column('payload', JSONB, nullable=False), Column('created', Float, nullable=False))
receipts = Table('receipts', metadata, Column('id', String, primary_key=True), Column('created', Float, nullable=False))
publication = Table('publication', metadata, Column('id', Integer, primary_key=True), Column('generation', String, nullable=False), Column('published_at', Float, nullable=False), Column('newest_event', Float, nullable=False))
health = Table('health', metadata, Column('id', String, primary_key=True), Column('checked_at', Float, nullable=False), Column('state', String, nullable=False), Column('payload', JSONB, nullable=False))
health_history = Table('health_history', metadata, Column('id', String, primary_key=True), Column('service', String, nullable=False, index=True), Column('checked_at', Float, nullable=False, index=True), Column('state', String, nullable=False), Column('latency_ms', Float))
workers = Table('workers', metadata, Column('id', String, primary_key=True), Column('updated', Float, nullable=False), Column('payload', JSONB, nullable=False))


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.primary = create_engine(settings.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5, connect_args={'connect_timeout': 5})
        self.read = create_engine(settings.read_database_url, pool_pre_ping=True, pool_size=5, max_overflow=5, connect_args={'connect_timeout': 5})

    def validate_instance(self):
        with self.primary.connect() as conn:
            row = conn.execute(select(instances)).mappings().one()
            fingerprint = hashlib.sha256(self.settings.secret_key.get_secret_value().encode()).hexdigest()
            if row['id'] != self.settings.instance_id or row['secret_fingerprint'] != fingerprint:
                raise RuntimeError('Database belongs to another instance or secret; use its original configuration')

    @contextmanager
    def writer(self):
        with self.primary.begin() as conn:
            # All aggregate/state mutations share a transaction lock, including recurrence.
            conn.execute(text('SELECT pg_advisory_xact_lock(7262401)'))
            yield conn

    def heartbeat(self, name: str, payload: dict):
        from sqlalchemy.dialects.postgresql import insert
        with self.primary.begin() as conn:
            conn.execute(insert(workers).values(id=name, updated=time.time(), payload=payload)
                         .on_conflict_do_update(index_elements=['id'], set_={'updated': time.time(), 'payload': payload}))

    def close(self):
        self.primary.dispose()
        self.read.dispose()
