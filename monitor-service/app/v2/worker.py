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
                next_publish = 0
                while not stop.is_set():
                    started = time.monotonic()
                    owner.execute(
                        text("SELECT 1")
                    )  # A lost ownership connection stops this worker.
                    check_services(store)
                    store.heartbeat("health", {"state": "running"})
                    if started >= next_publish:
                        try:
                            # Drain pending batches; each publication remains an atomic read snapshot.
                            while publish(store) == 100 and not stop.is_set():
                                pass
                            store.heartbeat("publisher", {"state": "running"})
                            next_publish = started + settings.publish_seconds
                        except Exception:
                            log.exception("Publication unavailable; primary outbox retained")
                            next_publish = started + 30
                    stop.wait(max(0, 30 - (time.monotonic() - started)))
    finally:
        if collector:
            collector.close()
        store.close()
