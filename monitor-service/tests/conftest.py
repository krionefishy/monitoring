import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).parents[1]))
from app.v2.config import Settings
from app.v2.migrate import migrate
from app.v2.storage import Store


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "application": "test",
                "environment": "test",
                "log_path": str(tmp_path / "access.json"),
                "rotated_glob": str(tmp_path / "access.json.[0-9]"),
            }
        )
    )
    return Settings(
        instance_id="test-instance",
        secret_key="a" * 48,
        database_url=os.getenv(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://monitor:test@127.0.0.1:56432/monitoring_v2_test",
        ),
        read_database_url=os.getenv(
            "TEST_READ_DATABASE_URL",
            "postgresql+psycopg://monitor:test@127.0.0.1:56433/monitoring_v2_test",
        ),
        redis_url=os.getenv("TEST_REDIS_URL", "redis://127.0.0.1:56479/15"),
        config_path=config,
        cookie_secure=False,
    )


@pytest.fixture
def store(settings):
    if os.getenv("MONITOR_TEST_DATABASES") != "1":
        pytest.skip("Set MONITOR_TEST_DATABASES=1 only for disposable PostgreSQL databases")
    for url in (settings.database_url, settings.read_database_url):
        if not url.endswith("/monitoring_v2_test"):
            raise RuntimeError("Tests require a disposable database named monitoring_v2_test")
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()
    result = Store(settings)
    migrate(result)
    yield result
    result.close()
