

import errno
import os
import posixpath
import stat

import mfusepy as fuse

from .client import ChecksumMismatch

KIND_BITS = {"file": stat.S_IFREG, "dir": stat.S_IFDIR, "symlink": stat.S_IFLNK}
A_DAY = 24 * 3600


class LazyFS(fuse.Operations):
    use_ns = True

    def __init__(self, index, cache):
        self.entries = {entry["path"]: entry for entry in index["entries"]}
        self.children = {}
        for path in self.entries:
            if path != "/":
                self.children.setdefault(posixpath.dirname(path), []).append(posixpath.basename(path))
        self.cache = cache
        self.uid, self.gid = os.getuid(), os.getgid()

    def entry(self, path):
        try:
            return self.entries[path]
        except KeyError:
            raise fuse.FuseOSError(errno.ENOENT) from None

    def getattr(self, path, fh=None):
        entry = self.entry(path)
        size = len(entry["target"].encode()) if entry["kind"] == "symlink" else entry["size"]
        mtime = entry["mtime"] * 10**9
        return {
            "st_mode": KIND_BITS[entry["kind"]] | entry["mode"],
            "st_nlink": 2 if entry["kind"] == "dir" else 1,
            "st_uid": self.uid,
            "st_gid": self.gid,
            "st_size": size,
            "st_atime": mtime,
            "st_mtime": mtime,
            "st_ctime": mtime,
        }

    def readdir(self, path, fh):
        return [".", "..", *self.children.get(path, [])]

    def readlink(self, path):
        return self.entry(path)["target"]

    def open(self, path, flags):
        if flags & (os.O_WRONLY | os.O_RDWR):
            raise fuse.FuseOSError(errno.EROFS)
        try:
            local = self.cache.path(self.entry(path)["digest"])
        except (OSError, ChecksumMismatch) as error:  # network trouble, a 404, or bad content
            raise fuse.FuseOSError(errno.EIO) from error
        return os.open(local, os.O_RDONLY)

    def read(self, path, size, offset, fh):
        return os.pread(fh, size, offset)

    def release(self, path, fh):
        os.close(fh)


def mount(index, cache, mountpoint):
    fuse.FUSE(
        LazyFS(index, cache),
        mountpoint,
        foreground=True,
        ro=True,
        fsname="imagestore",
        default_permissions=True,
        kernel_cache=True,
        attr_timeout=A_DAY,
        entry_timeout=A_DAY,
        negative_timeout=A_DAY,
    )
