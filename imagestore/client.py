import hashlib
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class ChecksumMismatch(Exception):
    """The downloaded bytes don't match the checksum they were requested by."""


def push_tar(server_url, tar_path, name, tag):
    """Push an image, uploading only the files the server doesn't have yet.

    Returns (image, uploaded_bytes, skipped_bytes).
    """
    server_url = server_url.rstrip("/")
    with tarfile.open(tar_path, "r:*") as tar:
        index = {}  # path in the tar -> digest
        members = {}  # digest -> a tar member with that content
        for member in tar:
            if member.isfile():
                digest = _sha256(tar.extractfile(member))
                index[member.name] = digest
                members.setdefault(digest, member)
            elif member.islnk() and member.linkname in index:
                index[member.name] = index[member.linkname]

        missing = _post_json(f"{server_url}/api/blobs/missing/", {"digests": list(members)})["missing"]
        for digest in missing:
            member = members[digest]
            upload = Request(
                f"{server_url}/api/blobs/{digest}/",
                data=tar.extractfile(member),
                method="PUT",
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(member.size)},
            )
            with urlopen(upload, timeout=60):
                pass

    files = [{"path": path, "digest": digest} for path, digest in index.items()]
    image = _post_json(f"{server_url}/api/images/push/", {"name": name, "tag": tag, "files": files})["image"]
    uploaded = sum(members[digest].size for digest in missing)
    return image, uploaded, image["total_size"] - uploaded


class LazyImage:
    def __init__(self, server_url, image_id, cache_dir):
        self.server_url = server_url.rstrip("/")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        index = json.loads(self._get(f"/api/images/{image_id}/index/"))
        self.name = f"{index['image']['name']}:{index['image']['tag']}"
        self.total_size = index["image"]["total_size"]
        self.files = {}  # path -> {"path", "digest", "size"}
        for entry in index["files"]:
            # Digests become file names in the cache, so a bad one from the
            # server must not turn into something like "../../home/me/.ssh/id_rsa".
            if not DIGEST_RE.fullmatch(entry["digest"]):
                raise ValueError(f"bad digest for {entry['path']} in the index: {entry['digest']!r}")
            self.files[entry["path"]] = entry

        self.files_downloaded = 0
        self.bytes_downloaded = 0
        self.cache_hits = 0

    def read(self, path):
        """Return the contents of `path`, downloading it only if it isn't cached yet."""
        if path not in self.files:
            raise FileNotFoundError(path)
        digest = self.files[path]["digest"]

        cached = self.cache_dir / digest
        if cached.exists():
            self.cache_hits += 1
            return cached.read_bytes()

        data = self._get(f"/api/blobs/{digest}/")
        if hashlib.sha256(data).hexdigest() != digest:
            raise ChecksumMismatch(f"{path}: the downloaded data doesn't match its checksum")
        self._save_to_cache(cached, data)
        self.files_downloaded += 1
        self.bytes_downloaded += len(data)
        return data

    def _get(self, url_path):
        with urlopen(self.server_url + url_path, timeout=30) as response:
            return response.read()

    def _save_to_cache(self, path, data):
        # Same trick as the server: write a temp file, then rename it into place.
        with tempfile.NamedTemporaryFile(dir=self.cache_dir, delete=False) as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp.name, path)


def _post_json(url, payload):
    request = Request(
        url, data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def _sha256(stream):
    sha256 = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        sha256.update(chunk)
    return sha256.hexdigest()
