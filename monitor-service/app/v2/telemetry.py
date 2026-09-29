"""Nginx JSON contract and bounded, mergeable telemetry (no SDK dependency)."""
from bisect import bisect_left
from collections import defaultdict
import hashlib
import math
import re
from typing import Any

from pydantic import BaseModel, Field
from .config import AppConfig

# Milliseconds; final bin includes everything above the last finite boundary.
LATENCY_BOUNDS = [5, 10, 25, 50, 100, 200, 400, 750, 1000, 2000, 5000, 10000, 30000, 60000]


class Aggregate(BaseModel):
    bucket: int
    service: str
    group: str
    method: str
    route: str
    status: int = Field(ge=100, le=599)
    count: int = Field(ge=1)
    histogram: list[int]
    latency_sum: float
    latency_count: int
    first_seen: float
    last_seen: float
    samples: list[dict] = []


class Batch(BaseModel):
    id: str
    source: str
    config_hash: str
    application: str
    environment: str
    rows: list[Aggregate]
    invalid: int = 0
    excluded: int = 0


def key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


import json


def route_info(path: str, config: AppConfig) -> tuple[str, str, str]:
    path = path.split('?', 1)[0]
    base = config.base_path
    if base and not (path == base or path.startswith(base + '/')):
        raise LookupError('outside base path')
    path = path[len(base):] or '/'
    group = path.strip('/').split('/')[0] or 'root'
    service = next((s.id for s in sorted(config.services, key=lambda s: len(s.prefix), reverse=True)
                    if s.prefix == '/' or path == s.prefix.rstrip('/') or path.startswith(s.prefix.rstrip('/') + '/')), 'unmapped')
    for rule in config.route_rules:
        if re.fullmatch(rule.pattern, path):
            return service, group, rule.route
    # Never guess resource IDs; unmatched requests share one bounded route per group.
    return service, group, path if path == '/' else f'/{group}/[unmatched]'


def parse_record(raw: bytes, config: AppConfig) -> dict:
    obj = json.loads(raw)
    if str(obj.get('schema_version')) != '1':
        raise ValueError('unsupported schema')
    if obj.get('application') != config.application or obj.get('environment') != config.environment:
        raise ValueError('wrong application/environment')
    path = obj['path']
    if not isinstance(path, str) or not path.startswith('/') or len(path) > 4096:
        raise ValueError('invalid path')
    if path.split('?', 1)[0] in config.exclude_paths:
        raise LookupError('excluded')
    timestamp = float(obj['msec'])
    status = int(obj['status'])
    method = obj['method']
    if not math.isfinite(timestamp) or timestamp < 0 or not 100 <= status <= 599:
        raise ValueError('invalid time/status')
    if method not in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'CONNECT', 'TRACE'}:
        method = 'OTHER'
    latency = obj.get('request_time', '-')
    latency = None if latency == '-' else float(latency) * 1000
    if latency is not None and (not math.isfinite(latency) or latency < 0):
        raise ValueError('invalid latency')
    service, group, route = route_info(path, config)
    # IDs are the only per-request sample; never keep raw paths/query strings/IPs.
    request_id = str(obj.get('request_id', ''))
    request_id = request_id if re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', request_id) else ''
    return dict(bucket=int(timestamp // 30) * 30, service=service, group=group[:128], method=method,
                route=route, status=status, time=timestamp, latency=latency, request_id=request_id)


def aggregate(records: list[dict]) -> list[Aggregate]:
    groups: dict[tuple, list] = defaultdict(list)
    for item in records:
        groups[tuple(item[k] for k in ('bucket', 'service', 'group', 'method', 'route', 'status'))].append(item)
    rows = []
    for dims, items in groups.items():
        histogram = [0] * (len(LATENCY_BOUNDS) + 1)
        total = 0.0
        for item in items:
            if item['latency'] is not None:
                histogram[bisect_left(LATENCY_BOUNDS, item['latency'])] += 1
                total += item['latency']
        rows.append(Aggregate(**dict(zip(('bucket', 'service', 'group', 'method', 'route', 'status'), dims)),
            count=len(items), histogram=histogram, latency_sum=total, latency_count=sum(histogram),
            first_seen=min(i['time'] for i in items), last_seen=max(i['time'] for i in items),
            samples=[{'time': i['time'], 'request_id': i['request_id']} for i in items[-3:] if i['request_id']] if dims[-1] >= 400 else []))
    return rows


def percentile(histogram: list[int], quantile: float) -> float | None:
    total = sum(histogram)
    if not total:
        return None
    target, current = math.ceil(total * quantile), 0
    for index, count in enumerate(histogram):
        current += count
        if current >= target:
            return LATENCY_BOUNDS[index] if index < len(LATENCY_BOUNDS) else None
    return None
