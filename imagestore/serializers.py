from django.conf import settings
from rest_framework import serializers

from .models import Image

DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Image
        fields = ["id", "name", "tag", "file_count", "total_size", "created_at"]


class ImageNameSerializer(serializers.Serializer):
    name = serializers.RegexField(r"^[a-z0-9]+([._/-][a-z0-9]+)*$", max_length=200)
    tag = serializers.RegexField(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$", max_length=100, default="latest")


class ImageUploadSerializer(ImageNameSerializer):
    file = serializers.FileField()


class IndexEntrySerializer(serializers.Serializer):
    path = serializers.CharField(max_length=4096, trim_whitespace=False)
    digest = serializers.RegexField(DIGEST_PATTERN)


class ImagePushSerializer(ImageNameSerializer):
    files = IndexEntrySerializer(many=True)


class MissingBlobsSerializer(serializers.Serializer):
    digests = serializers.ListField(
        child=serializers.RegexField(DIGEST_PATTERN), max_length=settings.IMAGE_MAX_ENTRIES
    )
