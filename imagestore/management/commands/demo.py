"""Pushes two versions of a fake Python app image, then starts both with LazyImage.

Everything goes through the HTTP API, so run `python3 manage.py runserver` in
another terminal first.
"""

import io
import json
import random
import tarfile
import tempfile
from pathlib import Path
from urllib.request import urlopen

from django.core.management.base import BaseCommand, CommandError

from imagestore.client import LazyImage, push_tar

IMAGE_NAME = "demo/webapp"

STARTUP_MODULES = [
    "abc", "base64", "codecs", "collections", "datetime", "encodings", "functools",
    "hashlib", "http", "io", "json", "logging", "os", "pathlib", "random", "re",
    "socket", "ssl", "sys", "typing",
]
UNUSED_MODULES = [
    "asyncio", "concurrent", "csv", "ctypes", "curses", "doctest", "email", "idlelib",
    "lib2to3", "multiprocessing", "pdb", "pydoc", "sqlite3", "tarfile", "tkinter",
    "turtle", "unittest", "xml", "zipfile", "zoneinfo",
]
LANGUAGES = ["de", "es", "fr", "it", "ja", "ko", "nl", "pl", "pt", "ru", "sv", "tr", "uk", "zh"]

STARTUP_FILES = [
    "/usr/bin/python3",
    "/usr/lib/libssl.so.3",
    "/etc/os-release",
    *[f"/usr/lib/python3.12/{module}.py" for module in STARTUP_MODULES],
    "/app/main.py",
    "/app/settings.py",
]


def sample_image(version):
    rng = random.Random(2024)
    files = {
        "/etc/os-release": b'NAME="Demo Linux"\nVERSION="12"\n',
        "/usr/bin/python3": rng.randbytes(2_500_000),
        "/usr/lib/libssl.so.3": rng.randbytes(700_000),
        "/usr/lib/libpython3.12.a": rng.randbytes(9_000_000),
    }
    for module in STARTUP_MODULES + UNUSED_MODULES:
        files[f"/usr/lib/python3.12/{module}.py"] = rng.randbytes(rng.randint(5_000, 60_000))
    for number in range(400):
        files[f"/usr/lib/python3.12/test/test_{number:03}.py"] = rng.randbytes(rng.randint(5_000, 60_000))
    for language in LANGUAGES:
        files[f"/usr/share/locale/{language}/LC_MESSAGES/python.mo"] = rng.randbytes(300_000)
    for number in range(300):
        files[f"/usr/share/doc/package-{number:03}/changelog.gz"] = rng.randbytes(rng.randint(1_000, 20_000))

    files["/app/main.py"] = f'print("Hello from {IMAGE_NAME}:{version}")\n'.encode()
    files["/app/settings.py"] = b"DEBUG = False\n"
    if version == "2.0":  # a new app version plus a security update in the base
        files["/etc/os-release"] = b'NAME="Demo Linux"\nVERSION="12.1"\n'
        files["/usr/lib/libssl.so.3"] = random.Random("libssl-fix").randbytes(700_000)
    return files


def write_tar(path, files):
    with tarfile.open(path, "w") as tar:
        for file_path, content in files.items():
            info = tarfile.TarInfo(file_path.lstrip("/"))
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))


def mb(size):
    return f"{size / 1_000_000:.1f} MB"


def stored_size(server, images):
    # Every distinct blob counted once, which is what the server keeps on disk.
    sizes = {}
    for image in images:
        with urlopen(f"{server}/api/images/{image['id']}/index/", timeout=30) as response:
            for entry in json.load(response)["entries"]:
                if entry["kind"] == "file":
                    sizes[entry["digest"]] = entry["size"]
    return sum(sizes.values())


class Command(BaseCommand):
    help = "Push two versions of a sample image, start them lazily, and show how much data moved."

    def add_arguments(self, parser):
        parser.add_argument(
            "--server", default="http://127.0.0.1:8000", help="URL of the running API server (default: %(default)s)"
        )

    def handle(self, *args, **options):
        server = options["server"].rstrip("/")
        try:
            urlopen(f"{server}/api/stats/", timeout=5).close()
        except OSError:
            raise CommandError(
                f"Can't reach the API at {server}. Start it in another terminal first:\n    python3 manage.py runserver"
            )

        with tempfile.TemporaryDirectory() as work_dir:
            images = self.push_images(server, Path(work_dir))
            self.start_images(server, images, Path(work_dir) / "machine-cache")

    def push_images(self, server, work_dir):
        self.stdout.write(
            f"\nStep 1: push two versions of {IMAGE_NAME}. Only files the server doesn't have are uploaded.\n"
        )
        images = []
        for version in ("1.0", "2.0"):
            tar_path = work_dir / f"webapp-{version}.tar"
            write_tar(tar_path, sample_image(version))
            image, uploaded, skipped = push_tar(server, tar_path, IMAGE_NAME, version)
            images.append(image)
            self.stdout.write(
                f"  {IMAGE_NAME}:{version}   {image['file_count']:,} files, {mb(image['total_size'])}"
                f"   uploaded: {mb(uploaded):>7}   already on the server: {mb(skipped):>7}"
            )
            if version == "1.0" and uploaded == 0:
                self.stdout.write("  (Nothing to upload for 1.0: an earlier run already pushed these files.)")

        both = sum(image["total_size"] for image in images)
        on_disk = stored_size(server, images)
        self.stdout.write(f"\n  Both images together: {mb(both)} of files, but only {mb(on_disk)} stored on disk.\n")
        return images

    def start_images(self, server, images, cache_dir):
        self.stdout.write(
            f"\nStep 2: start both versions on a fresh machine. Starting the app reads {len(STARTUP_FILES)} files.\n"
        )
        for image in images:
            lazy = LazyImage(server, image["id"], cache_dir)
            for path in STARTUP_FILES:
                lazy.read(path)
            share = f"({lazy.bytes_downloaded / lazy.total_size:.0%})"
            self.stdout.write(
                f"  {lazy.name}   downloaded {mb(lazy.bytes_downloaded):>6} of {mb(lazy.total_size)} {share:<5}"
                f"   files found in the machine's cache: {lazy.cache_hits}"
            )
        full = mb(images[0]["total_size"])
        self.stdout.write(f"\n  A normal pull would download the whole image ({full}) before the app could start.\n")
