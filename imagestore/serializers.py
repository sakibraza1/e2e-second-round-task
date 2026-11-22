from django.conf import settings
from rest_framework import serializers

from .models import Image, ImageFile

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
    kind = serializers.ChoiceField(ImageFile.Kind.choices, default=ImageFile.Kind.FILE)
    mode = serializers.IntegerField(min_value=0, max_value=0o7777, default=0o644)
    mtime = serializers.IntegerField(default=0)
    target = serializers.CharField(max_length=4096, trim_whitespace=False, allow_blank=True, default="")
    digest = serializers.RegexField(DIGEST_PATTERN, allow_null=True, default=None)

    def validate(self, data):
        if data["kind"] == ImageFile.Kind.FILE and not data["digest"]:
            raise serializers.ValidationError("A file needs a digest.")
        if data["kind"] == ImageFile.Kind.SYMLINK and not data["target"]:
            raise serializers.ValidationError("A symlink needs a target.")
        if data["kind"] != ImageFile.Kind.FILE:
            data["digest"] = None
        return data


class ImagePushSerializer(ImageNameSerializer):
    entries = IndexEntrySerializer(many=True)


class MissingBlobsSerializer(serializers.Serializer):
    digests = serializers.ListField(
        child=serializers.RegexField(DIGEST_PATTERN), max_length=settings.IMAGE_MAX_ENTRIES
    )
