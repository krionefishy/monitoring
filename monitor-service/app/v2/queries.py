import time
from collections import defaultdict

from sqlalchemy import func, select

from .storage import (
    health,
    health_history,
    metrics,
    workers,
)
from .telemetry import LATENCY_BOUNDS, percentile


def filters(table, service="", group="", route="", method=""):
    result = []
    for column, value in (
        (table.c.service, service),
        (table.c.route_group, group),
        (table.c.route, route),
        (table.c.method, method),
    ):
        if value:
            result.append(column == value)
    return result


def analytics(conn, start: int, end: int, step: int, service="", group="", route="", method=""):
    bucket = (func.floor(metrics.c.bucket / step) * step).label("time")
    count = func.sum(metrics.c.payload["count"].as_integer()).label("count")
    hist = [
        func.sum(metrics.c.payload["histogram"][i].as_integer()).label(f"h{i}")
        for i in range(len(LATENCY_BOUNDS) + 1)
    ]
    stmt = (
        select(
            bucket,
            metrics.c.service,
            metrics.c.route_group,
            metrics.c.route,
            metrics.c.method,
            metrics.c.status,
            count,
            *hist,
        )
        .where(
            metrics.c.bucket >= start,
            metrics.c.bucket < end,
            *filters(metrics, service, group, route, method),
        )
        .group_by(
            bucket,
            metrics.c.service,
            metrics.c.route_group,
            metrics.c.route,
            metrics.c.method,
            metrics.c.status,
        )
    )
    series = {
        timestamp: {"time": timestamp, "codes": {}, "total": 0}
        for timestamp in range(start // step * step, end, step)
    }
    routes = {}
    total_hist = [0] * len(hist)
    total = errors = server_errors = 0
    codes = defaultdict(int)
    for row in conn.execute(stmt).mappings():
        n = int(row["count"])
        code = str(row["status"])
        point = series[int(row["time"])]
        point["codes"][code] = point["codes"].get(code, 0) + n
        point["total"] += n
        codes[code] += n
        total += n
        errors += n if row["status"] >= 400 else 0
        server_errors += n if row["status"] >= 500 else 0
        identity = (row["service"], row["route"], row["method"])
        entry = routes.setdefault(
            identity,
            dict(
                service=row["service"],
                group=row["route_group"],
                route=row["route"],
                method=row["method"],
                count=0,
                errors=0,
                codes={},
                histogram=[0] * len(hist),
            ),
        )
        entry["count"] += n
        entry["errors"] += n if row["status"] >= 400 else 0
        entry["codes"][code] = entry["codes"].get(code, 0) + n
        for i in range(len(hist)):
            value = int(row[f"h{i}"] or 0)
            total_hist[i] += value
            entry["histogram"][i] += value
    duration = (end - start) / 60
    for entry in routes.values():
        entry["rpm"] = entry["count"] / duration
        route_hist = entry.pop("histogram")
        entry["p50"] = percentile(route_hist, 0.50)
        entry["p75"] = percentile(route_hist, 0.75)
        entry["p95"] = percentile(route_hist, 0.95)
        entry["latency_overflow"] = route_hist[-1]
    return dict(
        total=total,
        errors=errors,
        server_errors=server_errors,
        rpm=total / duration,
        p50=percentile(total_hist, 0.50),
        p75=percentile(total_hist, 0.75),
        p95=percentile(total_hist, 0.95),
        p99=percentile(total_hist, 0.99),
        latency_overflow=total_hist[-1],
        codes=dict(sorted(codes.items())),
        series=list(series.values()),
        step=step,
        routes=sorted(routes.values(), key=lambda r: r["count"], reverse=True),
        start=start,
        end=end,
    )


def service_status(store):
    config = store.settings.application()
    now = time.time()
    with store.primary.connect() as conn:
        current = {r["id"]: r for r in conn.execute(select(health)).mappings()}
        history = (
            conn.execute(
                select(health_history)
                .where(health_history.c.checked_at >= now - 86400)
                .order_by(health_history.c.checked_at)
            )
            .mappings()
            .all()
        )
    result = []
    for service in config.services:
        row = current.get(service.id)
        entries = [r for r in history if r["service"] == service.id]
        # Missing checks are unknown, never silently counted as healthy availability.
        checked = len(entries)
        healthy = sum(r["state"] == "healthy" for r in entries)
        result.append(
            dict(
                id=service.id,
                name=service.name,
                configured=bool(service.health_url),
                state=row["state"]
                if row and service.health_url and row["checked_at"] >= now - 90
                else "unknown",
                checked_at=row["checked_at"] if row else None,
                latency_ms=row["payload"].get("latency_ms") if row else None,
                reason=row["payload"].get("reason") if row else None,
                availability=healthy / checked * 100 if checked else None,
                coverage=min(100, checked / 2880 * 100),
                history=[{"time": r["checked_at"], "state": r["state"]} for r in entries[-60:]],
            )
        )
    return result


def diagnostics(store):
    with store.primary.connect() as conn:
        rows = conn.execute(select(workers)).mappings().all()
    return [dict(id=r["id"], updated=r["updated"], **r["payload"]) for r in rows]
