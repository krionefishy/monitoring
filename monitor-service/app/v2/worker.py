import logging
import signal
import threading
import time

from sqlalchemy import text

from .collector import Collector
from .config import Settings
from .health import check_services
from .ingest import ingest
from .projection import publish
from .storage import Store

log = logging.getLogger(__name__)


def run(role: str):
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    store = Store(settings)
    store.validate_instance()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    collector = (
        Collector(settings.application(), settings.spool_path, settings.spool_max_mb)
        if role == "collector"
        else None
    )
    publisher = None
    try:
        if collector:
            while not stop.is_set():
                started = time.monotonic()
                info = collector.scan()
                try:
                    for batch_id, batch in collector.pending():
                        ingest(store, batch)
                        collector.acknowledge(batch_id)
                    store.heartbeat(role, info)
                except Exception:
                    log.exception("Ingest unavailable; retaining durable batches")
                stop.wait(max(0, 30 - (time.monotonic() - started)))
        else:
            # Session-level lock prevents multiple health schedulers across processes.
            with store.primary.connect() as owner:
                if not owner.execute(text("SELECT pg_try_advisory_lock(7262403)")).scalar():
                    raise RuntimeError("Another scheduler is already active")
                owner.commit()

                def publish_loop():
                    while not stop.is_set():
                        delay = settings.publish_seconds
                        try:
                            while publish(store) == 100 and not stop.is_set():
                                pass
                            store.heartbeat("publisher", {"state": "running"})
                        except Exception:
                            log.exception("Publication unavailable; primary outbox retained")
                            delay = 30
                        stop.wait(delay)

                publisher = threading.Thread(
                    target=publish_loop, name="read-publisher", daemon=True
                )
                publisher.start()
                while not stop.is_set():
                    started = time.monotonic()
                    # Loss of the session holding the singleton lock stops both schedules.
                    owner.execute(text("SELECT 1"))
                    owner.commit()
                    check_services(store)
                    store.heartbeat("health", {"state": "running"})
                    stop.wait(max(0, 30 - (time.monotonic() - started)))
    finally:
        stop.set()
        if publisher:
            publisher.join(timeout=10)
        if collector:
            collector.close()
        store.close()
