from pathlib import Path
import hashlib
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from .storage import Store, instances, publication


def migrate(store: Store):
    for engine in (store.primary, store.read):
        config = Config()
        config.set_main_option('script_location', str(Path(__file__).parent / 'migrations'))
        config.set_main_option('sqlalchemy.url', engine.url.render_as_string(hide_password=False).replace('%', '%%'))
        command.upgrade(config, 'head')
        with engine.begin() as conn:
            existing = conn.execute(select(instances.c.id)).scalar_one_or_none()
            if existing and existing != store.settings.instance_id:
                raise RuntimeError('Refusing to initialize another instance database')
            conn.execute(insert(instances).values(id=store.settings.instance_id, secret_fingerprint=hashlib.sha256(store.settings.secret_key.get_secret_value().encode()).hexdigest()).on_conflict_do_nothing())
            conn.execute(insert(publication).values(id=1, generation='initial', published_at=0, newest_event=0).on_conflict_do_nothing())
    store.validate_instance()
