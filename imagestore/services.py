"""Turning an uploaded image into blobs plus an index.

Two ways in: import_tar() takes the whole tar in one go, push_image() takes just
the index after the client has uploaded the blobs the server was missing.
"""

import tarfile

from django.conf import settings
from django.db import transaction

from . import storage
from .models import Blob, Image, ImageFile


class InvalidImage(Exception):
    pass


class MissingBlobs(InvalidImage):
    def __init__(self, digests):
        super().__init__(f"{len(digests)} blob(s) haven't been uploaded yet.")
        self.digests = digests


def clean_path(name):
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise InvalidImage(f"Unsafe or empty path: {name!r}")
    return "/" + "/".join(parts)


def import_tar(tar_file, name, tag):
    files = {}
    new_digests = set()
    unpacked = 0
    try:
        with tarfile.open(fileobj=tar_file, mode="r:*") as tar, transaction.atomic():
            for count, member in enumerate(tar, start=1):
                # Limits are checked from the tar headers, before anything is written.
                if count > settings.IMAGE_MAX_ENTRIES:
                    raise InvalidImage(f"The tar file has more than {settings.IMAGE_MAX_ENTRIES:,} entries.")
                if member.isfile():
                    path = clean_path(member.name)
                    unpacked += member.size
                    if unpacked > settings.IMAGE_MAX_SIZE:
                        raise InvalidImage(f"The image is larger than {settings.IMAGE_MAX_SIZE:,} bytes unpacked.")
                    digest, size = storage.save_blob(tar.extractfile(member))
                    blob, created = Blob.objects.get_or_create(digest=digest, defaults={"size": size})
                    if created:
                        new_digests.add(digest)
                    files[path] = blob  # if a path appears twice, the later one wins
                elif member.islnk():
                    target = clean_path(member.linkname)
                    if target in files:
                        files[clean_path(member.name)] = files[target]

            image = save_image(name, tag, files)
    except (tarfile.TarError, EOFError):
        raise InvalidImage("The uploaded file is not a readable tar archive.") from None

    blobs_in_image = {blob.digest: blob for blob in files.values()}
    new_bytes = sum(blob.size for digest, blob in blobs_in_image.items() if digest in new_digests)
    stats = {
        "files": image.file_count,
        "total_bytes": image.total_size,
        "new_bytes_stored": new_bytes,
        "bytes_saved_by_dedup": image.total_size - new_bytes,
    }
    return image, stats


def push_image(name, tag, entries):
    """Create an image from [{"path", "digest"}, ...] whose blobs are already on the server."""
    if len(entries) > settings.IMAGE_MAX_ENTRIES:
        raise InvalidImage(f"The index has more than {settings.IMAGE_MAX_ENTRIES:,} entries.")
    index = {}
    for entry in entries:
        path = clean_path(entry["path"])
        if path in index:
            raise InvalidImage(f"{path} is listed twice.")
        index[path] = entry["digest"]

    blobs = Blob.objects.in_bulk(set(index.values()))
    missing = sorted(set(index.values()) - blobs.keys())
    if missing:
        raise MissingBlobs(missing)

    files = {path: blobs[digest] for path, digest in index.items()}
    if sum(blob.size for blob in files.values()) > settings.IMAGE_MAX_SIZE:
        raise InvalidImage(f"The image is larger than {settings.IMAGE_MAX_SIZE:,} bytes unpacked.")
    with transaction.atomic():
        return save_image(name, tag, files)


def save_image(name, tag, files):
    # Pushing an existing name:tag again replaces it, like a registry does.
    Image.objects.filter(name=name, tag=tag).delete()
    image = Image.objects.create(
        name=name,
        tag=tag,
        file_count=len(files),
        total_size=sum(blob.size for blob in files.values()),
    )
    ImageFile.objects.bulk_create([ImageFile(image=image, path=path, blob=blob) for path, blob in files.items()])
    return image
