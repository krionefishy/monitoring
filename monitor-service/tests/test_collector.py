import json
import os
import time

import pytest
from app.v2.collector import Collector
from app.v2.config import AppConfig, RouteRule, Service
from app.v2.telemetry import aggregate, parse_record, percentile, route_info


def line(status=200, path="/home/profile", timestamp=None):
    return (
        json.dumps(
            {
                "schema_version": "1",
                "application": "test",
                "environment": "test",
                "msec": str(timestamp or time.time()),
                "status": str(status),
                "method": "GET",
                "path": path,
                "request_time": "0.12",
                "request_id": "request-1",
            }
        ).encode()
        + b"\n"
    )


def test_route_mapping_and_queries():
    config = AppConfig(
        application="test",
        environment="test",
        base_path="/api/v1",
        services=[
            Service(id="fallback", name="Fallback"),
            Service(id="home", name="Home", prefix="/home"),
        ],
        route_rules=[RouteRule(pattern=r"/home/users/[0-9]+", route="/home/users/{id}")],
    )
    assert route_info("/api/v1/home/users/42?token=secret", config) == (
        "home",
        "home",
        "/home/users/{id}",
    )
    assert route_info("/api/v1/home/profile", config)[2] == "/home/profile"
    with pytest.raises(LookupError):
        route_info("/api/v10/home", config)


def test_exact_statuses_and_merged_histogram():
    config = AppConfig(application="test", environment="test")
    records = [parse_record(line(code), config) for code in (200, 404, 409, 500, 404)]
    result = aggregate(records)
    assert {r.status: r.count for r in result} == {200: 1, 404: 2, 409: 1, 500: 1}
    hist = [sum(r.histogram[i] for r in result) for i in range(len(result[0].histogram))]
    assert percentile(hist, 0.95) == 200
    assert all("path" not in sample for row in result for sample in row.samples)


def test_partial_restart_rotation_and_truncation(tmp_path):
    path = tmp_path / "access.json"
    spool = tmp_path / "spool.sqlite"
    config = AppConfig(
        application="test",
        environment="test",
        log_path=str(path),
        rotated_glob=str(path) + ".[0-9]",
    )
    first = line(404)
    path.write_bytes(first + line(409)[:20])
    collector = Collector(config, spool)
    collector.scan()
    initial = list(collector.pending())
    assert sum(r.count for _, b in initial for r in b.rows) == 1
    collector.close()
    with path.open("ab") as f:
        f.write(line(409)[20:])
    collector = Collector(config, spool)
    collector.scan()
    assert sum(r.count for _, b in collector.pending() for r in b.rows) == 2
    os.rename(path, str(path) + ".1")
    path.write_bytes(line(500))
    collector.scan()
    assert sum(r.count for _, b in collector.pending() for r in b.rows) == 3
    collector.scan()
    assert len(list(collector.pending())) == 3
    path.write_bytes(line(201))
    collector.scan()
    assert sum(r.count for _, b in collector.pending() for r in b.rows) == 4
    collector.close()


def test_exclusive_reader_and_malformed_line(tmp_path):
    path = tmp_path / "access.json"
    path.write_bytes(b"{bad json}\n" + line(200))
    config = AppConfig(
        application="test",
        environment="test",
        log_path=str(path),
        rotated_glob=str(path) + ".[0-9]",
    )
    collector = Collector(config, tmp_path / "spool")
    with pytest.raises(RuntimeError):
        Collector(config, tmp_path / "spool")
    info = collector.scan()
    assert info["invalid_lines"] == 1
    assert info["requests"] == 1
    collector.close()
