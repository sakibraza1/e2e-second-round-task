"""Turning an uploaded image into blobs plus an index, and back into a tar.

Two ways in: import_tar() takes the whole tar in one go, push_image() takes just
the index after the client has uploaded the blobs the server was missing.
One way out: export_tar() streams a whole image, for clients that pull everything up front.
"""

import posixpath
import tarfile

from django.conf import settings
from django.db import transaction

from . import storage
from .models import Blob, Image, ImageFile

FILE, DIR, SYMLINK = ImageFile.Kind.FILE, ImageFile.Kind.DIR, ImageFile.Kind.SYMLINK


class InvalidImage(Exception):
    pass


class MissingBlobs(InvalidImage):
    def __init__(self, digests):
        super().__init__(f"{len(digests)} blob(s) haven't been uploaded yet.")
        self.digests = digests


def clean_path(name):
    # "./usr/bin/" becomes "/usr/bin", and "./" becomes "/".
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise InvalidImage(f"Unsafe path: {name!r}")
    return "/" + "/".join(parts)


def import_tar(tar_file, name, tag):
    entries = {}
    new_digests = set()
    unpacked = 0
    try:
        with tarfile.open(fileobj=tar_file, mode="r:*") as tar, transaction.atomic():
            for count, member in enumerate(tar, start=1):
                # Limits are checked from the tar headers, before anything is written.
                if count > settings.IMAGE_MAX_ENTRIES:
                    raise InvalidImage(f"The tar file has more than {settings.IMAGE_MAX_ENTRIES:,} entries.")
                entry = ImageFile(path=clean_path(member.name), mode=member.mode & 0o7777, mtime=int(member.mtime))
                if member.isdir():
                    entry.kind = DIR
                elif member.issym():
                    entry.kind, entry.target = SYMLINK, member.linkname
                elif member.islnk():
                    # A hard link is the same file under another name, so it shares the blob.
                    linked = entries.get(clean_path(member.linkname))
                    if linked is None or linked.kind != FILE:
                        continue
                    entry.blob, entry.mode, entry.mtime = linked.blob, linked.mode, linked.mtime
                elif member.isfile():
                    unpacked += member.size
                    if unpacked > settings.IMAGE_MAX_SIZE:
                        raise InvalidImage(f"The image is larger than {settings.IMAGE_MAX_SIZE:,} bytes unpacked.")
                    digest, size = storage.save_blob(tar.extractfile(member))
                    entry.blob, created = Blob.objects.get_or_create(digest=digest, defaults={"size": size})
                    if created:
                        new_digests.add(digest)
                else:
                    continue  # device files and fifos don't belong in an image
                entries[entry.path] = entry  # if a path appears twice, the later one wins

            image = save_image(name, tag, entries)
    except (tarfile.TarError, EOFError):
        raise InvalidImage("The uploaded file is not a readable tar archive.") from None

    blobs_in_image = {entry.blob.digest: entry.blob for entry in entries.values() if entry.kind == FILE}
    new_bytes = sum(blob.size for digest, blob in blobs_in_image.items() if digest in new_digests)
    stats = {
        "files": image.file_count,
        "total_bytes": image.total_size,
        "new_bytes_stored": new_bytes,
        "bytes_saved_by_dedup": image.total_size - new_bytes,
    }
    return image, stats


def push_image(name, tag, entries):
    """Create an image from index entries whose blobs are already on the server."""
    if len(entries) > settings.IMAGE_MAX_ENTRIES:
        raise InvalidImage(f"The index has more than {settings.IMAGE_MAX_ENTRIES:,} entries.")
    index = {}
    for data in entries:
        entry = ImageFile(
            path=clean_path(data["path"]),
            kind=data["kind"],
            mode=data["mode"],
            mtime=data["mtime"],
            target=data["target"],
            blob_id=data["digest"],
        )
        if entry.path in index:
            raise InvalidImage(f"{entry.path} is listed twice.")
        index[entry.path] = entry

    digests = {entry.blob_id for entry in index.values() if entry.kind == FILE}
    blobs = Blob.objects.in_bulk(digests)
    missing = sorted(digests - blobs.keys())
    if missing:
        raise MissingBlobs(missing)
    for entry in index.values():
        entry.blob = blobs.get(entry.blob_id)  # None for folders and symlinks

    if sum(entry.blob.size for entry in index.values() if entry.kind == FILE) > settings.IMAGE_MAX_SIZE:
        raise InvalidImage(f"The image is larger than {settings.IMAGE_MAX_SIZE:,} bytes unpacked.")
    with transaction.atomic():
        return save_image(name, tag, index)


def save_image(name, tag, entries):
    add_missing_folders(entries)
    files = [entry for entry in entries.values() if entry.kind == FILE]

    # Pushing an existing name:tag again replaces it, like a registry does.
    Image.objects.filter(name=name, tag=tag).delete()
    image = Image.objects.create(
        name=name,
        tag=tag,
        file_count=len(files),
        total_size=sum(entry.blob.size for entry in files),
    )
    for entry in entries.values():
        entry.image = image
    ImageFile.objects.bulk_create(list(entries.values()))
    return image


def add_missing_folders(entries):
    # A tar doesn't have to list every folder, but a mounted file system needs all of them.
    for path in list(entries):
        parent = posixpath.dirname(path)
        while parent not in entries:
            entries[parent] = ImageFile(path=parent, kind=DIR, mode=0o755)
            parent = posixpath.dirname(parent)
    for path in entries:
        parent = entries[posixpath.dirname(path)]
        if parent.kind != DIR:
            raise InvalidImage(f"{parent.path} has things inside it but isn't a folder.")


def export_tar(image):
    """Yield the whole image as a plain tar, folders first, the way a full pull downloads it."""
    out = _Chunks()
    with tarfile.open(fileobj=out, mode="w|") as tar:
        for entry in image.files.select_related("blob").iterator():
            info = tarfile.TarInfo(entry.path.lstrip("/") or ".")
            info.mode, info.mtime = entry.mode, entry.mtime
            if entry.kind == FILE:
                info.size = entry.blob.size
                with storage.open_blob(entry.blob_id) as blob:
                    tar.addfile(info, blob)
            else:
                info.type = tarfile.DIRTYPE if entry.kind == DIR else tarfile.SYMTYPE
                info.linkname = entry.target
                tar.addfile(info)
            yield out.take()
    yield out.take()  # the end-of-archive marker written by close()


class _Chunks:
    # tarfile wants a file to write into. This one just keeps what was written until it's taken.
    def __init__(self):
        self.parts = []

    def write(self, data):
        self.parts.append(bytes(data))
        return len(data)

    def take(self):
        data = b"".join(self.parts)
        self.parts.clear()
        return data
