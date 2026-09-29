from pathlib import Path
from typing import Literal
import hashlib
import json
import re

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RouteRule(BaseModel):
    pattern: str
    route: str

    @field_validator('pattern')
    @classmethod
    def valid_pattern(cls, value: str) -> str:
        re.compile(value)
        return value


class Service(BaseModel):
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]+$')
    name: str
    prefix: str = '/'
    health_url: str | None = None
    expected_status: int = Field(default=200, ge=100, le=599)
    # Optional JSON equality check, e.g. {"status": "ok"}.
    health_body: dict | None = None

    @field_validator('health_url')
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if value and not value.startswith(('http://', 'https://')):
            raise ValueError('health_url must use HTTP or HTTPS')
        return value


class AppConfig(BaseModel):
    application: str = Field(default='application', pattern=r'^[a-zA-Z0-9_-]+$')
    environment: str = Field(default='production', pattern=r'^[a-zA-Z0-9_-]+$')
    topology: Literal['direct', 'gateway'] = 'direct'
    base_path: str = ''
    log_path: str = '/var/log/nginx/monitoring.json'
    rotated_glob: str = '/var/log/nginx/monitoring.json.[0-9]'
    exclude_paths: list[str] = ['/health', '/healthz', '/readyz']
    route_rules: list[RouteRule] = []
    services: list[Service] = [Service(id='application', name='Application')]

    @model_validator(mode='after')
    def consistent(self):
        self.base_path = '/' + self.base_path.strip('/') if self.base_path.strip('/') else ''
        ids = [s.id for s in self.services]
        if len(ids) != len(set(ids)):
            raise ValueError('service ids must be unique')
        for service in self.services:
            if not service.prefix.startswith('/'):
                raise ValueError('service prefix must start with /')
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='MONITOR_', env_file='.env', extra='ignore')
    instance_id: str = Field(min_length=3, pattern=r'^[a-zA-Z0-9_-]+$')
    secret_key: SecretStr = Field(min_length=32)
    database_url: str
    read_database_url: str
    redis_url: str = 'redis://redis:6379/0'
    config_path: Path = Path('/app/config.json')
    spool_path: Path = Path('/app/data/collector.sqlite')
    cookie_secure: bool = True
    session_hours: int = Field(default=12, ge=1, le=168)
    publish_seconds: int = Field(default=300, ge=30)
    retention_days: int = Field(default=30, ge=1, le=365)
    spool_max_mb: int = Field(default=512, ge=16)
    frontend_path: Path = Path('/app/frontend')

    @field_validator('database_url', 'read_database_url')
    @classmethod
    def postgres_only(cls, value: str) -> str:
        if not value.startswith('postgresql+psycopg://'):
            raise ValueError('v2 requires a separate PostgreSQL database using psycopg')
        return value

    @model_validator(mode='after')
    def separate_databases(self):
        if self.database_url == self.read_database_url:
            raise ValueError('primary and read database must be separate')
        return self

    def application(self) -> AppConfig:
        return AppConfig.model_validate(json.loads(self.config_path.read_text()))
