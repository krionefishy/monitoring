"""Exercise the shipped Compose on disposable volumes, including its real worker loops."""

import argparse
import http.cookiejar
import json
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def smoke(directory):
    project = "monitoring-smoke-" + secrets.token_hex(4)
    directory.mkdir(parents=True, exist_ok=True)
    logs = directory / "logs"
    logs.mkdir()
    config = json.loads((ROOT / "monitor-service/deploy/application.direct.json").read_text())
    config["services"][0]["health_url"] = "http://api:8000/healthz"
    config_path = directory / "config.json"
    config_path.write_text(json.dumps(config))
    with (logs / "monitoring.json").open("w") as output:
        for status in [200, 200, 404, 409, 500]:
            output.write(
                json.dumps(
                    dict(
                        schema_version="1",
                        application="application",
                        environment="production",
                        msec=str(time.time() - 120),
                        method="GET",
                        path="/home/profile",
                        status=str(status),
                        request_time="0.1",
                        request_id="smoke",
                    )
                )
                + "\n"
            )
    env = {
        "MONITOR_INSTANCE_ID": project,
        "MONITOR_SECRET_KEY": secrets.token_urlsafe(48),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "POSTGRES_READ_PASSWORD": secrets.token_urlsafe(24),
        "MONITOR_CONFIG_FILE": str(config_path),
        "NGINX_LOG_DIRECTORY": str(logs),
        "MONITOR_PORT": "18189",
        "MONITOR_COOKIE_SECURE": "false",
        "MONITOR_PUBLIC_ORIGIN": "http://127.0.0.1:18189",
        "MONITOR_TRUSTED_PROXIES": "127.0.0.1,::1",
        "MONITOR_PUBLISH_SECONDS": "30",
    }
    env_path = directory / "runtime.env"
    env_path.write_text("\n".join(f"{key}={value}" for key, value in env.items()) + "\n")
    env_path.chmod(0o600)
    override = directory / "image.json"
    override.write_text(
        json.dumps(
            {
                "services": {
                    service: {"image": project + ":test"}
                    for service in ["init", "api", "collector", "worker"]
                }
            }
        )
    )
    compose = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(env_path),
        "-f",
        str(ROOT / "monitor-service/deploy/docker-compose.yaml"),
        "-f",
        str(override),
    ]

    def run(*args, **kwargs):
        return subprocess.run(compose + list(args), check=True, text=True, timeout=600, **kwargs)

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def request(path, body=None):
        encoded = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            env["MONITOR_PUBLIC_ORIGIN"] + path,
            data=encoded,
            headers={"Content-Type": "application/json"},
        )
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    try:
        run("build", "api")
        run("up", "-d", "--no-build", "--wait", "--wait-timeout", "120")
        proxy_config = run(
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            'import uvicorn; print(uvicorn.Config("main:app").forwarded_allow_ips)',
            capture_output=True,
        ).stdout.strip()
        assert proxy_config == env["MONITOR_TRUSTED_PROXIES"]
        password = secrets.token_urlsafe(24)
        run(
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "app.v2.cli",
            "create-user",
            "smoke",
            input=f"{password}\n{password}\n",
            capture_output=True,
        )
        try:
            request("/api/metrics")
            raise AssertionError("Anonymous metrics were exposed")
        except urllib.error.HTTPError as error:
            assert error.code == 401
        assert (
            request("/api/auth/login", {"username": "smoke", "password": password})["username"]
            == "smoke"
        )
        deadline = time.monotonic() + 100
        while True:
            metrics = request("/api/metrics?hours=1")
            health = request("/api/services-status")["services"][0]["state"]
            if metrics["total"] == 5 and health == "healthy":
                break
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"Collector/publication/health did not converge: {metrics['total']}, {health}"
                )
            time.sleep(2)
        assert metrics["codes"] == {"200": 2, "404": 1, "409": 1, "500": 1}
        assert [metrics[key] for key in ("p50", "p75", "p95")] == [100, 100, 100]
        assert [metrics["routes"][0][key] for key in ("p50", "p75", "p95")] == [100, 100, 100]
        assert request("/api/incidents")["total"] == 3
        print(
            "Compose smoke passed: local login, collector, exact codes, incidents, publication, health.",
            flush=True,
        )
    except BaseException:
        subprocess.run(compose + ["logs", "--tail", "80"], check=False, timeout=30)
        raise
    finally:
        subprocess.run(compose + ["down", "--volumes", "--remove-orphans"], check=True, timeout=90)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory", type=Path, help="New scratch directory shared with the Docker host"
    )
    args = parser.parse_args()
    if args.directory:
        smoke(args.directory.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="monitoring-smoke-") as directory:
            smoke(Path(directory))
