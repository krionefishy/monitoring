"""Independent PostgreSQL v2 schema. Never opens or changes the v1 SQLite file."""
from alembic import op
revision = 'v2_0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''
    CREATE TABLE instances (id varchar PRIMARY KEY, secret_fingerprint varchar NOT NULL);
    CREATE TABLE users (id varchar PRIMARY KEY, username varchar UNIQUE NOT NULL, password_hash text NOT NULL, role varchar NOT NULL, enabled boolean NOT NULL);
    CREATE TABLE sessions (token_hash varchar PRIMARY KEY, user_id varchar NOT NULL REFERENCES users(id), instance_id varchar NOT NULL, csrf varchar NOT NULL, expires double precision NOT NULL);
    CREATE INDEX sessions_expires ON sessions(expires);
    CREATE TABLE metrics (id varchar PRIMARY KEY, bucket bigint NOT NULL, service varchar NOT NULL, route_group varchar NOT NULL, method varchar NOT NULL, route text NOT NULL, status integer NOT NULL, payload jsonb NOT NULL);
    CREATE INDEX metrics_bucket ON metrics(bucket);
    CREATE INDEX metrics_route ON metrics(service, route_group, bucket);
    CREATE TABLE incidents (id varchar PRIMARY KEY, service varchar NOT NULL, route_group varchar NOT NULL, method varchar NOT NULL, route text NOT NULL, status integer NOT NULL, payload jsonb NOT NULL);
    CREATE TABLE incident_states (id varchar PRIMARY KEY, state varchar NOT NULL, resolved_through bigint NOT NULL DEFAULT 0, updated double precision NOT NULL, updated_by varchar NOT NULL, version integer NOT NULL DEFAULT 1);
    CREATE TABLE batches (id varchar PRIMARY KEY, digest varchar NOT NULL, created double precision NOT NULL);
    CREATE TABLE outbox (id varchar PRIMARY KEY, payload jsonb NOT NULL, created double precision NOT NULL);
    CREATE TABLE receipts (id varchar PRIMARY KEY, created double precision NOT NULL);
    CREATE TABLE publication (id integer PRIMARY KEY, generation varchar NOT NULL, published_at double precision NOT NULL, newest_event double precision NOT NULL);
    CREATE TABLE health (id varchar PRIMARY KEY, checked_at double precision NOT NULL, state varchar NOT NULL, payload jsonb NOT NULL);
    CREATE TABLE health_history (id varchar PRIMARY KEY, service varchar NOT NULL, checked_at double precision NOT NULL, state varchar NOT NULL, latency_ms double precision);
    CREATE INDEX health_history_time ON health_history(service, checked_at);
    CREATE TABLE workers (id varchar PRIMARY KEY, updated double precision NOT NULL, payload jsonb NOT NULL);
    ''')


def downgrade():
    raise RuntimeError('Restore a PostgreSQL backup to downgrade; telemetry is not discarded automatically')
