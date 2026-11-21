import hashlib
import os
import re
import tempfile
from pathlib import Path

from django.conf import settings

CHUNK_SIZE = 1024 * 1024  # read 1 MB at a time, so big files never sit in memory
DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class DigestMismatch(ValueError):
    pass


def blob_path(digest):
    # The digest ends up in a filesystem path, so only a real sha256 hex string gets through.
    if not DIGEST_RE.fullmatch(digest):
        raise ValueError(f"not a sha256 digest: {digest!r}")
    return Path(settings.BLOB_STORAGE_DIR) / digest[:2] / digest


def save_blob(stream, expected_digest=None):
    root = Path(settings.BLOB_STORAGE_DIR)
    root.mkdir(parents=True, exist_ok=True)

    # The checksum is only known after reading everything, so write to a
    # temporary file first and give it its real name at the end.
    sha256 = hashlib.sha256()
    size = 0
    tmp = tempfile.NamedTemporaryFile(dir=root, delete=False)
    try:
        with tmp:
            while chunk := stream.read(CHUNK_SIZE):
                sha256.update(chunk)
                tmp.write(chunk)
                size += len(chunk)
            digest = sha256.hexdigest()
            if expected_digest and digest != expected_digest:
                raise DigestMismatch(f"expected {expected_digest}, got {digest}")
            final_path = blob_path(digest)
            is_new = not final_path.exists()
            if is_new:
                # An existing blob is never rewritten, so its bytes have to be on
                # disk before the rename. Otherwise a crash can leave it broken for good.
                tmp.flush()
                os.fsync(tmp.fileno())
        if is_new:
            final_path.parent.mkdir(exist_ok=True)
            os.replace(tmp.name, final_path)
    finally:
        if os.path.exists(tmp.name):  # content we already had, or an error
            os.remove(tmp.name)
    return digest, size


def open_blob(digest):
    return open(blob_path(digest), "rb")
