"""Minimal ClickHouse (HTTP interface) and S3 object-store clients."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import httpx

from hivemarket.observatory.schema import STATEMENTS
from hivemarket.observatory.settings import ObservatorySettings


class ClickHouse:
    def __init__(self, url: str, user: str, password: str, database: str = "hivemarket",
                 transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self.database = database
        self.client = httpx.Client(timeout=60.0, transport=transport,
                                   headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password})

    @classmethod
    def from_settings(cls, s: ObservatorySettings) -> "ClickHouse":
        return cls(s.clickhouse_url, s.clickhouse_user, s.clickhouse_password)

    def ping(self) -> bool:
        try:
            return self.client.get(f"{self.url}/ping").status_code == 200
        except httpx.HTTPError:
            return False

    def execute(self, sql: str, body: bytes | None = None, settings: dict[str, Any] | None = None) -> str:
        params = {"database": self.database, **(settings or {})}
        if body is None:
            r = self.client.post(self.url, params=params, content=sql.encode())
        else:
            r = self.client.post(self.url, params={**params, "query": sql}, content=body)
        if r.status_code != 200:
            raise RuntimeError(f"ClickHouse error {r.status_code}: {r.text[:500]}")
        return r.text

    def insert(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        lines = [json.dumps(r, ensure_ascii=False, default=str) for r in rows]
        if not lines:
            return 0
        self.execute(f"INSERT INTO {table} FORMAT JSONEachRow", ("\n".join(lines) + "\n").encode(),
                     settings={"date_time_input_format": "best_effort", "input_format_skip_unknown_fields": 1})
        return len(lines)

    def delete_run(self, table: str, run_id: str) -> None:
        self.execute(f"DELETE FROM {table} WHERE run_id = '{run_id.replace(chr(39), '')}'")

    def query_json(self, sql: str) -> list[dict[str, Any]]:
        text = self.execute(sql + " FORMAT JSONEachRow")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def apply_schema(self) -> None:
        for stmt in STATEMENTS:
            self.client.post(self.url, content=stmt.encode()).raise_for_status()


class ObjectStore:
    """Thin wrapper over the MinIO client (any S3-compatible store works)."""

    def __init__(self, endpoint: str, access_key: str, secret_key: str, secure: bool = False):
        from minio import Minio

        self.client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)

    @classmethod
    def from_settings(cls, s: ObservatorySettings) -> "ObjectStore":
        return cls(s.s3_endpoint, s.s3_access_key, s.s3_secret_key, s.s3_secure)

    def ensure_bucket(self, bucket: str) -> None:
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def put_file(self, bucket: str, key: str, path: Path) -> None:
        self.client.fput_object(bucket, key, str(path))

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str = "application/json") -> None:
        import io

        self.client.put_object(bucket, key, io.BytesIO(data), len(data), content_type=content_type)

    def list(self, bucket: str, prefix: str = "") -> Iterable[tuple[str, int, str]]:
        for obj in self.client.list_objects(bucket, prefix=prefix, recursive=True):
            yield obj.object_name, obj.size, obj.etag

    def download(self, bucket: str, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.client.fget_object(bucket, key, str(dest))
