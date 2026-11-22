from django.db import models


class Blob(models.Model):
    digest = models.CharField(max_length=64, primary_key=True)  # SHA-256 checksum, hex
    size = models.BigIntegerField()  # in bytes

    def __str__(self):
        return f"{self.digest[:12]} ({self.size} bytes)"


class Image(models.Model):
    name = models.CharField(max_length=200)
    tag = models.CharField(max_length=100)
    file_count = models.PositiveIntegerField(default=0)
    total_size = models.BigIntegerField(default=0)  # in bytes
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "tag"]
        constraints = [
            models.UniqueConstraint(fields=["name", "tag"], name="unique_image_name_and_tag"),
        ]

    def __str__(self):
        return f"{self.name}:{self.tag}"


class ImageFile(models.Model):
    # One entry of an image's file system. Like in Unix, folders and symlinks are files too.
    class Kind(models.TextChoices):
        FILE = "file"
        DIR = "dir"
        SYMLINK = "symlink"

    image = models.ForeignKey(Image, on_delete=models.CASCADE, related_name="files")
    path = models.CharField(max_length=4096)
    kind = models.CharField(max_length=7, choices=Kind.choices, default=Kind.FILE)
    mode = models.PositiveIntegerField(default=0o644)  # permission bits
    # Kept because Python only trusts its cached .pyc files when the .py file's mtime matches.
    mtime = models.BigIntegerField(default=0)
    target = models.CharField(max_length=4096, blank=True, default="")  # symlinks only
    blob = models.ForeignKey(Blob, on_delete=models.PROTECT, related_name="image_files", null=True, blank=True)

    class Meta:
        ordering = ["path"]
        constraints = [
            models.UniqueConstraint(fields=["image", "path"], name="unique_path_per_image"),
        ]

    def __str__(self):
        return f"{self.image}{self.path}"
