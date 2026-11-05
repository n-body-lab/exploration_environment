"""`hivemarket observatory up|down|status` and `hivemarket backup`."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from hivemarket.observatory.clients import ClickHouse, ObjectStore
from hivemarket.observatory.settings import ObservatorySettings, data_dir, ensure_credentials, read_env_file

COMPOSE_FILE = Path(__file__).resolve().parents[2] / "observatory" / "docker-compose.yml"
PROJECT = "hivemarket-observatory"


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    root = data_dir()
    env = {**os.environ, "HIVEMARKET_DATA": str(root), "HM_UID": str(os.getuid()), "HM_GID": str(os.getgid())}
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", PROJECT,
           "--env-file", str(root / "credentials.env"), *args]
    return subprocess.run(cmd, env=env, check=check)


def write_s3_identities(root: Path) -> None:
    creds = read_env_file(root / "credentials.env")
    identities = {"identities": [{
        "name": "hivemarket",
        "credentials": [{"accessKey": creds["S3_ACCESS_KEY"], "secretKey": creds["S3_SECRET_KEY"]}],
        "actions": ["Admin", "Read", "Write", "List", "Tagging"],
    }]}
    path = root / "s3.json"
    path.write_text(json.dumps(identities, indent=2))
    os.chmod(path, 0o644)  # read by the store's container user


def _retry(fn, what: str, attempts: int = 60):
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if i == attempts - 1:
                raise RuntimeError(f"{what} not ready: {e!r}") from e
            time.sleep(1)


def up() -> ObservatorySettings:
    root = data_dir()
    ensure_credentials(root)
    write_s3_identities(root)
    for sub in ("clickhouse", "clickhouse-logs", "s3", "grafana"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    _compose("up", "-d", "--wait")
    s = ObservatorySettings.load()
    ch = ClickHouse.from_settings(s)
    for _ in range(60):
        if ch.ping():
            break
        time.sleep(1)
    _retry(ch.apply_schema, "clickhouse")
    store = ObjectStore.from_settings(s)
    _retry(lambda: store.ensure_bucket(s.bucket), "object store")
    store.ensure_bucket(s.backup_bucket)
    print(f"observatory up. data: {root}\n"
          f"  grafana    {s.grafana_url}  (user admin, password GRAFANA_ADMIN_PASSWORD in {root / 'credentials.env'})\n"
          f"  clickhouse {s.clickhouse_url}\n"
          f"  s3         http://{s.s3_endpoint}  (bucket {s.bucket})")
    return s


def down() -> None:
    # Data is in bind mounts under the data dir; stopping/removing containers never touches it.
    _compose("down")
    print(f"observatory stopped. data kept in {data_dir()}")


def available(s: ObservatorySettings | None = None) -> bool:
    s = s or ObservatorySettings.load()
    return bool(s.clickhouse_password) and ClickHouse.from_settings(s).ping()


def status() -> None:
    s = ObservatorySettings.load()
    if not available(s):
        print("observatory: not reachable (run `hivemarket observatory up`)")
        return
    ch = ClickHouse.from_settings(s)
    runs = ch.query_json("SELECT count() AS runs FROM hivemarket.runs FINAL")[0]["runs"]
    events = ch.query_json("SELECT count() AS events FROM hivemarket.events")[0]["events"]
    print(f"observatory: up. {runs} runs, {events} events. grafana {s.grafana_url}")


def backup(dest: Path) -> None:
    """ClickHouse BACKUP into the backup bucket, then mirror both buckets to `dest`."""
    s = ObservatorySettings.load()
    ch = ClickHouse.from_settings(s)
    name = time.strftime("%Y%m%d-%H%M%S")
    # Runs inside the observatory network, so ClickHouse reaches the store by service name.
    ch.execute(f"BACKUP DATABASE hivemarket TO S3('http://s3:8333/{s.backup_bucket}/clickhouse/{name}/', "
               f"'{s.s3_access_key}', '{s.s3_secret_key}')")
    print(f"clickhouse backup written to s3://{s.backup_bucket}/clickhouse/{name}/")
    store = ObjectStore.from_settings(s)
    copied = 0
    for bucket in (s.bucket, s.backup_bucket):
        for key, size, _ in store.list(bucket):
            target = dest / bucket / key
            if target.exists() and target.stat().st_size == size:
                continue
            store.download(bucket, key, target)
            copied += 1
    print(f"mirrored {copied} new/changed objects to {dest}")
