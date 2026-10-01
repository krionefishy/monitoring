import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.v2 import health
from app.v2.config import Service


def test_slow_body_has_total_deadline(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.08)  # Each read is faster than the timeout; full body is not.
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(health, "PROBE_TIMEOUT", 0.25)
    try:
        service = Service(
            id="slow",
            name="Slow",
            health_url=f"http://127.0.0.1:{server.server_port}",
            health_body={"status": "ok"},
        )
        started = time.monotonic()
        ok, _, reason = asyncio.run(health.probe(service))
        assert not ok
        assert reason == "Превышено время проверки health"
        assert time.monotonic() - started < 1.0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_fast_service_published_before_slow_service_finishes(monkeypatch):
    from types import SimpleNamespace

    async def scenario():
        saved_fast = threading.Event()

        async def probe(service):
            if service.id == "slow":
                assert await asyncio.to_thread(saved_fast.wait, 2), (
                    "Fast result was held by slow probe"
                )
            return True, 1, None

        def save_result(store, service, result):
            if service.id == "fast":
                saved_fast.set()

        monkeypatch.setattr(health, "probe", probe)
        monkeypatch.setattr(health, "save_result", save_result)
        services = [
            Service(id=i, name=i, health_url="http://example.test/health") for i in ["slow", "fast"]
        ]
        store = SimpleNamespace(
            settings=SimpleNamespace(application=lambda: SimpleNamespace(services=services))
        )
        await health.check_services_async(store)
        assert saved_fast.is_set()

    asyncio.run(scenario())
