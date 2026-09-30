"""
core/storage.py

Object storage for raw experience artifacts.

Defines the StorageClient protocol so tests inject a mock without touching
MinIO. The production implementation is MinIOStorageClient.

URI format:  minio://<bucket>/<key>
             e.g. minio://raw/experiences/<owner_id>/<content_hash>

Object storage is append-only from the brain's point of view: a given
content_hash is written once and never overwritten. PUTs to an existing
key are no-ops (the content is identical by construction because we key
on the SHA-256 of the text).
"""

from __future__ import annotations

import io
import os
from typing import Optional, Protocol
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Protocol — the interface both real and mock clients must satisfy
# ---------------------------------------------------------------------------

class StorageClient(Protocol):
    def put_experience(
        self, owner_id: str, content_hash: str, text: str
    ) -> str:
        """
        Store raw experience text. Returns the canonical content_uri.
        Idempotent: calling twice with the same arguments returns the same URI
        without a second write.
        """
        ...

    def get_text(self, content_uri: str) -> str:
        """Retrieve the raw text for a given content_uri."""
        ...


# ---------------------------------------------------------------------------
# URI helpers
# ---------------------------------------------------------------------------

def _experience_key(owner_id: str, content_hash: str) -> str:
    return f"experiences/{owner_id}/{content_hash}"


def _build_uri(bucket: str, key: str) -> str:
    return f"minio://{bucket}/{key}"


def _parse_uri(content_uri: str) -> tuple[str, str]:
    """Return (bucket, key) from a minio:// URI."""
    parsed = urlparse(content_uri)
    if parsed.scheme != "minio":
        raise ValueError(f"Expected minio:// URI, got: {content_uri!r}")
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")
    return bucket, key


# ---------------------------------------------------------------------------
# Production implementation
# ---------------------------------------------------------------------------

class MinIOStorageClient:
    """
    Wraps the MinIO Python SDK. Bucket is created on first use.
    endpoint must NOT include the http:// scheme (the SDK adds it).
    """

    BUCKET = "raw"

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool = False,
    ) -> None:
        from minio import Minio  # imported here so tests load without the package
        # strip scheme if present — Minio() expects host:port
        clean = endpoint.replace("https://", "").replace("http://", "")
        self._client = Minio(clean, access_key=access_key, secret_key=secret_key, secure=secure)
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self.BUCKET):
            self._client.make_bucket(self.BUCKET)

    def put_experience(self, owner_id: str, content_hash: str, text: str) -> str:
        from minio.error import S3Error
        key = _experience_key(owner_id, content_hash)
        # Check for existing object first to keep the write idempotent.
        try:
            self._client.stat_object(self.BUCKET, key)
            return _build_uri(self.BUCKET, key)   # already present
        except S3Error as e:
            if e.code != "NoSuchKey":
                raise
        data = text.encode("utf-8")
        self._client.put_object(
            self.BUCKET,
            key,
            io.BytesIO(data),
            len(data),
            content_type="text/plain; charset=utf-8",
        )
        return _build_uri(self.BUCKET, key)

    def get_text(self, content_uri: str) -> str:
        bucket, key = _parse_uri(content_uri)
        response = self._client.get_object(bucket, key)
        try:
            return response.read().decode("utf-8")
        finally:
            response.close()
            response.release_conn()


def from_env() -> MinIOStorageClient:
    """Construct a MinIOStorageClient from environment variables."""
    return MinIOStorageClient(
        endpoint=os.environ.get("MINIO_ENDPOINT", "http://localhost:9000"),
        access_key=os.environ.get("MINIO_ACCESS_KEY", "brain"),
        secret_key=os.environ.get("MINIO_SECRET_KEY", "brainbrain"),
    )


# ---------------------------------------------------------------------------
# Mock implementation for tests
# ---------------------------------------------------------------------------

class MockStorageClient:
    """
    In-memory storage for tests. No network calls, no MinIO required.
    Exposes _store so tests can inspect what was written.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def put_experience(self, owner_id: str, content_hash: str, text: str) -> str:
        key = _experience_key(owner_id, content_hash)
        uri = _build_uri("raw", key)
        if uri not in self._store:   # idempotent: first write wins
            self._store[uri] = text
        return uri

    def get_text(self, content_uri: str) -> str:
        if content_uri not in self._store:
            raise KeyError(f"Object not found: {content_uri!r}")
        return self._store[content_uri]
