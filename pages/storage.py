"""Screenshot storage abstraction.

Provides a single interface for storing, retrieving, and deleting screenshot
artefacts regardless of whether they live on the local filesystem or in an
S3-compatible bucket.

Usage
-----
    from pages.storage import screenshot_storage as storage

    # Write a file
    storage.save("9/abc123.jpg", jpeg_bytes)

    # Check existence
    storage.exists("9/abc123.jpg")

    # Delete
    storage.delete("9/abc123.jpg")

    # Get a URL that can be returned to the browser (pre-signed for S3,
    # or a local /api/screenshots/… path for disk storage)
    url = storage.url("9/abc123.jpg")

    # Open for reading (returns a file-like object)
    with storage.open("9/abc123.jpg") as f:
        data = f.read()
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import BinaryIO

from django.conf import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class ScreenshotStorageBase:
    """Common interface all backends must implement."""

    def save(self, rel_path: str, data: bytes) -> None:
        raise NotImplementedError

    def open(self, rel_path: str) -> BinaryIO:
        raise NotImplementedError

    def exists(self, rel_path: str) -> bool:
        raise NotImplementedError

    def delete(self, rel_path: str) -> None:
        raise NotImplementedError

    def url(self, rel_path: str) -> str:
        """Return a URL the browser can use to fetch the image."""
        raise NotImplementedError

    def local_path(self, rel_path: str) -> Path | None:
        """Return the absolute local Path if available, else None.

        Used by Pillow operations that need a real filesystem path.
        For S3, this downloads the file to a temp location and returns that.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Local-disk backend
# ---------------------------------------------------------------------------

class LocalScreenshotStorage(ScreenshotStorageBase):
    """Stores artefacts on the local filesystem under SCREENSHOTS_DIR."""

    def _root(self) -> Path:
        root = Path(getattr(settings, "SCREENSHOTS_DIR",
                            os.path.join(settings.BASE_DIR, "screenshots")))
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _abs(self, rel_path: str) -> Path:
        return self._root() / rel_path

    def save(self, rel_path: str, data: bytes) -> None:
        abs_path = self._abs(rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(data)

    def open(self, rel_path: str) -> BinaryIO:
        return open(self._abs(rel_path), "rb")

    def exists(self, rel_path: str) -> bool:
        return self._abs(rel_path).is_file()

    def delete(self, rel_path: str) -> None:
        if not rel_path:
            return
        try:
            p = self._abs(rel_path)
            if p.is_file():
                p.unlink()
        except Exception:
            logger.exception("Failed to delete local screenshot: %s", rel_path)

    def url(self, rel_path: str) -> str:
        return f"/api/screenshots/{rel_path}"

    def local_path(self, rel_path: str) -> Path | None:
        p = self._abs(rel_path)
        return p if p.is_file() else None


# ---------------------------------------------------------------------------
# S3 backend
# ---------------------------------------------------------------------------

class S3ScreenshotStorage(ScreenshotStorageBase):
    """Stores artefacts in an S3-compatible bucket using boto3 directly.

    We use boto3 directly (no django-storages for screenshots) so that we can
    issue pre-signed GET URLs without any additional Django middleware.
    """

    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        self.bucket     = settings.S3_BUCKET_NAME
        self.prefix     = settings.S3_KEY_PREFIX   # e.g. "screenshots"
        self.expiry     = settings.S3_PRESIGN_EXPIRY
        endpoint        = settings.S3_ENDPOINT_URL or None
        path_style      = getattr(settings, "S3_USE_PATH_STYLE", False)

        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if path_style else "auto"},
        )

        self._client = boto3.client(
            "s3",
            region_name=settings.S3_REGION_NAME,
            endpoint_url=endpoint,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            config=config,
        )
        logger.info(
            "S3ScreenshotStorage initialised (bucket=%s prefix=%s endpoint=%s)",
            self.bucket, self.prefix, endpoint or "AWS",
        )

    def _key(self, rel_path: str) -> str:
        """Convert a relative path like ``9/abc.jpg`` to an S3 object key."""
        rel = rel_path.replace("\\", "/").lstrip("/")
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def save(self, rel_path: str, data: bytes) -> None:
        key = self._key(rel_path)
        content_type = "image/jpeg" if rel_path.lower().endswith(".jpg") else "image/png"
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
            logger.debug("S3 upload: s3://%s/%s", self.bucket, key)
        except Exception:
            logger.exception("S3 upload failed: %s", key)
            raise

    def open(self, rel_path: str) -> BinaryIO:
        key = self._key(rel_path)
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"]

    def exists(self, rel_path: str) -> bool:
        from botocore.exceptions import ClientError
        key = self._key(rel_path)
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def delete(self, rel_path: str) -> None:
        if not rel_path:
            return
        key = self._key(rel_path)
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
            logger.debug("S3 delete: s3://%s/%s", self.bucket, key)
        except Exception:
            logger.exception("S3 delete failed: %s", key)

    def url(self, rel_path: str) -> str:
        """Return a pre-signed GET URL valid for S3_PRESIGN_EXPIRY seconds."""
        key = self._key(rel_path)
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.expiry,
            )
        except Exception:
            logger.exception("Failed to generate pre-signed URL for %s", key)
            return ""

    def local_path(self, rel_path: str) -> Path | None:
        """Download the S3 object to a temp file and return its path.

        The caller is responsible for deleting the temp file when done.
        """
        import tempfile
        key = self._key(rel_path)
        try:
            suffix = Path(rel_path).suffix or ".jpg"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            self._client.download_fileobj(self.bucket, key, tmp)
            tmp.flush()
            tmp.close()
            return Path(tmp.name)
        except Exception:
            logger.exception("S3 download to temp failed: %s", key)
            return None


# ---------------------------------------------------------------------------
# SeaweedFS S3-compatible backend
# ---------------------------------------------------------------------------

class SeaweedFSScreenshotStorage(ScreenshotStorageBase):
    """Stores artefacts in a SeaweedFS cluster via its S3-compatible gateway.

    Key differences from the generic S3 backend:

    * **No pre-signed URLs** — SeaweedFS is an internal service; the browser
      should never talk to it directly.  ``url()`` returns a Django-proxied
      path (``/api/screenshots/<rel>``), exactly like local storage.  Django
      then streams the object from SeaweedFS in ``serve_screenshot()``.

    * **Path-style addressing** — SeaweedFS S3 requires it.

    * **Anonymous access supported** — if ``SEAWEEDFS_ACCESS_KEY`` is empty,
      boto3 is configured with dummy credentials so the unsigned request still
      reaches SeaweedFS (which can be configured to allow anonymous access in
      dev mode).
    """

    def __init__(self) -> None:
        import boto3
        from botocore.config import Config
        from botocore import UNSIGNED

        self.bucket = settings.SEAWEEDFS_BUCKET
        self.prefix = settings.SEAWEEDFS_KEY_PREFIX
        endpoint    = settings.SEAWEEDFS_ENDPOINT.rstrip("/")
        access_key  = settings.SEAWEEDFS_ACCESS_KEY
        secret_key  = settings.SEAWEEDFS_SECRET_KEY

        if access_key and secret_key:
            # Authenticated mode — credentials supplied via s3.config.json on SeaweedFS side
            config = Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            )
            self._client = boto3.client(
                "s3",
                region_name=settings.SEAWEEDFS_REGION,
                endpoint_url=endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=config,
            )
        else:
            # Anonymous / no-auth mode — send unsigned requests
            config = Config(
                signature_version=UNSIGNED,
                s3={"addressing_style": "path"},
            )
            self._client = boto3.client(
                "s3",
                region_name=settings.SEAWEEDFS_REGION,
                endpoint_url=endpoint,
                config=config,
            )

        # Ensure the bucket exists (idempotent).
        self._ensure_bucket()

        logger.info(
            "SeaweedFSScreenshotStorage initialised (endpoint=%s bucket=%s prefix=%s auth=%s)",
            endpoint, self.bucket, self.prefix, "yes" if access_key else "anonymous",
        )

    def _ensure_bucket(self) -> None:
        """Create the bucket if it does not already exist."""
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self._client.create_bucket(Bucket=self.bucket)
                logger.info("SeaweedFS: created bucket '%s'", self.bucket)
            except Exception:
                logger.warning(
                    "SeaweedFS: could not create bucket '%s' — it may already exist "
                    "or the gateway is not yet ready.", self.bucket, exc_info=True,
                )

    def _key(self, rel_path: str) -> str:
        rel = rel_path.replace("\\", "/").lstrip("/")
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def save(self, rel_path: str, data: bytes) -> None:
        key = self._key(rel_path)
        content_type = "image/jpeg" if rel_path.lower().endswith(".jpg") else "image/png"
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
            logger.debug("SeaweedFS upload: %s/%s", self.bucket, key)
        except Exception:
            logger.exception("SeaweedFS upload failed: %s", key)
            raise

    def open(self, rel_path: str) -> BinaryIO:
        key = self._key(rel_path)
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"]

    def exists(self, rel_path: str) -> bool:
        from botocore.exceptions import ClientError
        key = self._key(rel_path)
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("404", "NoSuchKey", "403", "Forbidden"):
                return False
            logger.warning("SeaweedFS exists() unexpected error for %s: %s", key, exc)
            return False
        except Exception as exc:
            logger.warning("SeaweedFS exists() failed for %s: %s", key, exc)
            return False

    def delete(self, rel_path: str) -> None:
        if not rel_path:
            return
        key = self._key(rel_path)
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
            logger.debug("SeaweedFS delete: %s/%s", self.bucket, key)
        except Exception:
            logger.exception("SeaweedFS delete failed: %s", key)

    def url(self, rel_path: str) -> str:
        """Return a Django-proxied URL — the browser never talks to SeaweedFS directly."""
        return f"/api/screenshots/{rel_path}"

    def local_path(self, rel_path: str) -> Path | None:
        """Download from SeaweedFS to a temp file for Pillow processing."""
        import tempfile
        key = self._key(rel_path)
        try:
            suffix = Path(rel_path).suffix or ".jpg"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            self._client.download_fileobj(self.bucket, key, tmp)
            tmp.flush()
            tmp.close()
            return Path(tmp.name)
        except Exception:
            logger.exception("SeaweedFS download to temp failed: %s", key)
            return None


# ---------------------------------------------------------------------------
# Singleton — use this throughout the codebase
# ---------------------------------------------------------------------------

def _make_storage() -> ScreenshotStorageBase:
    if getattr(settings, "USE_SEAWEEDFS_STORAGE", False):
        return SeaweedFSScreenshotStorage()
    if getattr(settings, "USE_S3_STORAGE", False):
        return S3ScreenshotStorage()
    return LocalScreenshotStorage()


# Module-level singleton; lazily re-created if the module is reloaded.
screenshot_storage: ScreenshotStorageBase = _make_storage()

