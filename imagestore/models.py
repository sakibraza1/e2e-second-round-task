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
    image = models.ForeignKey(Image, on_delete=models.CASCADE, related_name="files")
    path = models.CharField(max_length=4096)
    blob = models.ForeignKey(Blob, on_delete=models.PROTECT, related_name="image_files")

    class Meta:
        ordering = ["path"]
        constraints = [
            models.UniqueConstraint(fields=["image", "path"], name="unique_path_per_image"),
        ]

    def __str__(self):
        return f"{self.image}{self.path}"
