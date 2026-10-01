"""Stable outbox ordering and bounded route cardinality."""

from alembic import op

revision = "v2_0002"
down_revision = "v2_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE outbox ADD COLUMN sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE")
    op.execute("CREATE TABLE route_registry (id varchar PRIMARY KEY)")


def downgrade():
    raise RuntimeError("Restore a backup to downgrade")
