import hashlib
import json
import os
import re
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CHUNK_SIZE = 1024 * 1024
DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class ChecksumMismatch(Exception):
    """The downloaded bytes don't match the checksum they were requested by."""


class Link:
    def __init__(self, mbit_per_s=None, latency_ms=0):
        self.bytes_per_s = mbit_per_s * 125_000 if mbit_per_s else None
        self.latency = latency_ms / 1000

    def open(self, url):
        time.sleep(self.latency)
        return _Download(urlopen(url, timeout=60), self.bytes_per_s)


class _Download:
    def __init__(self, response, bytes_per_s):
        self.response = response
        self.bytes_per_s = bytes_per_s
        self.bytes = 0
        self.started = time.monotonic()

    def read(self, size=-1):
        data = self.response.read(size)
        self.bytes += len(data)
        if self.bytes_per_s:
            ahead = self.bytes / self.bytes_per_s - (time.monotonic() - self.started)
            if ahead > 0:
                time.sleep(ahead)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.response.close()


class BlobCache:
    """Blobs on local disk, named by their checksum.

    A blob is downloaded the first time it's needed and verified on the way in, so
    anything in the cache can be trusted. Because it's keyed by checksum, a file that
    appears in many images is downloaded once per machine.
    """

    def __init__(self, server_url, cache_dir, link=None):
        self.server_url = server_url.rstrip("/")
        # Absolute, because once a FUSE mount is running the working directory is "/".
        self.dir = Path(cache_dir).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.link = link or Link()
        self.lock = threading.Lock()
        self.blob_locks = {}  # one per digest, so two readers of the same blob share a download
        self.downloads = 0
        self.downloaded_bytes = 0
        self.hits = 0

    def path(self, digest):
        """Local path of the blob, downloading it first if it isn't here yet."""
        if not DIGEST_RE.fullmatch(digest):
            raise ValueError(f"not a sha256 digest: {digest!r}")
        path = self.dir / digest
        if path.exists():
            self.hits += 1
            return path
        with self._lock_for(digest):
            if path.exists():  # another thread downloaded it while we waited
                self.hits += 1
            else:
                self._download(digest, path)
        return path

    def _lock_for(self, digest):
        with self.lock:
            return self.blob_locks.setdefault(digest, threading.Lock())

    def _download(self, digest, path):
        sha256 = hashlib.sha256()
        tmp = tempfile.NamedTemporaryFile(dir=self.dir, delete=False)
        try:
            with tmp, self.link.open(f"{self.server_url}/api/blobs/{digest}/") as response:
                while chunk := response.read(CHUNK_SIZE):
                    sha256.update(chunk)
                    tmp.write(chunk)
                if sha256.hexdigest() != digest:
                    raise ChecksumMismatch(f"blob {digest}: the downloaded data doesn't match its checksum")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp.name, path)
        finally:
            if os.path.exists(tmp.name):
                os.remove(tmp.name)
        with self.lock:
            self.downloads += 1
            self.downloaded_bytes += response.bytes


def find_image(server_url, ref):
    """Look an image up by "name:tag" (the tag defaults to "latest")."""
    name, _, tag = ref.partition(":")
    query = urlencode({"name": name, "tag": tag or "latest"})
    with urlopen(f"{server_url.rstrip('/')}/api/images/?{query}", timeout=30) as response:
        results = json.load(response)["results"]
    if not results:
        raise LookupError(f"there's no image called {ref} on {server_url}")
    return results[0]


def fetch_index(server_url, image_id, link=None):
    """Download an image's index: every path, its kind, mode, mtime, and the file's digest."""
    with (link or Link()).open(f"{server_url.rstrip('/')}/api/images/{image_id}/index/") as response:
        index = json.loads(response.read())
    for entry in index["entries"]:
        # Digests become file names in the cache, so a bad one from the server must
        # not turn into something like "../../home/me/.ssh/id_rsa".
        if entry["kind"] == "file" and not DIGEST_RE.fullmatch(entry["digest"] or ""):
            raise ValueError(f"bad digest for {entry['path']} in the index: {entry['digest']!r}")
    return index


def pull_image(server_url, image_id, dest, link=None):
    url = f"{server_url.rstrip('/')}/api/images/{image_id}/tar/"
    with (link or Link()).open(url) as response, tarfile.open(fileobj=response, mode="r|") as tar:
        tar.extractall(dest, filter="tar")
    return response.bytes


def push_tar(server_url, tar_path, name, tag):
    server_url = server_url.rstrip("/")
    with tarfile.open(tar_path, "r:*") as tar:
        entries = {}
        members = {}
        for member in tar:
            entry = {"path": member.name, "mode": member.mode & 0o7777, "mtime": int(member.mtime)}
            if member.isdir():
                entry["kind"] = "dir"
            elif member.issym():
                entry.update(kind="symlink", target=member.linkname)
            elif member.islnk() and member.linkname in entries:
                entry = dict(entries[member.linkname], path=member.name)
            elif member.isfile():
                digest = _sha256(tar.extractfile(member))
                entry.update(kind="file", digest=digest)
                members.setdefault(digest, member)
            else:
                continue
            entries[member.name] = entry

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

    body = {"name": name, "tag": tag, "entries": list(entries.values())}
    image = _post_json(f"{server_url}/api/images/push/", body)["image"]
    uploaded = sum(members[digest].size for digest in missing)
    return image, uploaded, image["total_size"] - uploaded


class LazyImage:
    """Reads an image's files on demand, without mounting anything. The demo command uses it."""

    def __init__(self, server_url, image_id, cache_dir):
        index = fetch_index(server_url, image_id)
        self.name = f"{index['image']['name']}:{index['image']['tag']}"
        self.total_size = index["image"]["total_size"]
        self.files = {entry["path"]: entry for entry in index["entries"] if entry["kind"] == "file"}
        self.cache = BlobCache(server_url, cache_dir)

    @property
    def files_downloaded(self):
        return self.cache.downloads

    @property
    def bytes_downloaded(self):
        return self.cache.downloaded_bytes

    @property
    def cache_hits(self):
        return self.cache.hits

    def read(self, path):
        """Return the contents of `path`, downloading it only if it isn't cached yet."""
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.cache.path(self.files[path]["digest"]).read_bytes()


def _post_json(url, payload):
    request = Request(
        url, data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"}
    )
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def _sha256(stream):
    sha256 = hashlib.sha256()
    while chunk := stream.read(CHUNK_SIZE):
        sha256.update(chunk)
    return sha256.hexdigest()
