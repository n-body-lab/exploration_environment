"""Where the observatory lives and how to reach it.

Data and credentials live in $HIVEMARKET_DATA (default ~/hivemarket-observatory):
  credentials.env   generated once, chmod 600; also the observatory compose env file
  clickhouse/ minio/ grafana/   service data (bind mounts, survive container removal)
Environment variables HM_* override anything (e.g. a remote observatory).
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

CREDENTIAL_KEYS = ["CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD",
                   "GRAFANA_ADMIN_PASSWORD"]


def data_dir() -> Path:
    return Path(os.environ.get("HIVEMARKET_DATA", Path.home() / "hivemarket-observatory")).expanduser()


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def ensure_credentials(root: Path | None = None) -> Path:
    root = root or data_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "credentials.env"
    existing = read_env_file(path)
    defaults = {
        "CLICKHOUSE_USER": "hivemarket",
        "CLICKHOUSE_PASSWORD": secrets.token_urlsafe(18),
        "MINIO_ROOT_USER": "hivemarket",
        "MINIO_ROOT_PASSWORD": secrets.token_urlsafe(18),
        "GRAFANA_ADMIN_PASSWORD": secrets.token_urlsafe(12),
    }
    merged = {k: existing.get(k) or defaults[k] for k in CREDENTIAL_KEYS}
    if merged != existing:
        path.write_text("".join(f"{k}={v}\n" for k, v in merged.items()))
        os.chmod(path, 0o600)
    return path


@dataclass
class ObservatorySettings:
    clickhouse_url: str
    clickhouse_user: str
    clickhouse_password: str
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_secure: bool = False
    bucket: str = "hivemarket"
    backup_bucket: str = "hivemarket-backups"
    grafana_url: str = "http://127.0.0.1:3000"

    @classmethod
    def load(cls) -> "ObservatorySettings":
        creds = read_env_file(data_dir() / "credentials.env")
        env = os.environ.get
        return cls(
            clickhouse_url=env("HM_CLICKHOUSE_URL", "http://127.0.0.1:8123"),
            clickhouse_user=env("HM_CLICKHOUSE_USER", creds.get("CLICKHOUSE_USER", "hivemarket")),
            clickhouse_password=env("HM_CLICKHOUSE_PASSWORD", creds.get("CLICKHOUSE_PASSWORD", "")),
            s3_endpoint=env("HM_S3_ENDPOINT", "127.0.0.1:9000"),
            s3_access_key=env("HM_S3_ACCESS_KEY", creds.get("MINIO_ROOT_USER", "")),
            s3_secret_key=env("HM_S3_SECRET_KEY", creds.get("MINIO_ROOT_PASSWORD", "")),
            s3_secure=env("HM_S3_SECURE", "0") == "1",
            bucket=env("HM_S3_BUCKET", "hivemarket"),
            backup_bucket=env("HM_S3_BACKUP_BUCKET", "hivemarket-backups"),
            grafana_url=env("HM_GRAFANA_URL", "http://127.0.0.1:3000"),
        )
