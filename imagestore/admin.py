from django.contrib import admin

from .models import Blob, Image


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    list_display = ["name", "tag", "file_count", "total_size", "created_at"]


@admin.register(Blob)
class BlobAdmin(admin.ModelAdmin):
    list_display = ["digest", "size"]
