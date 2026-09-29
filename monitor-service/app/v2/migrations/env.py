from alembic import context
from sqlalchemy import create_engine

with create_engine(context.config.get_main_option("sqlalchemy.url")).connect() as connection:
    context.configure(connection=connection, version_table="v2_schema_version")
    with context.begin_transaction():
        context.run_migrations()
