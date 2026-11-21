from django.conf import settings
from django.db.models import Count, Sum
from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from . import storage
from .models import Blob, Image, ImageFile
from .serializers import ImagePushSerializer, ImageSerializer, ImageUploadSerializer, MissingBlobsSerializer
from .services import InvalidImage, MissingBlobs, import_tar, push_image


class ImageViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Image.objects.all()
    parser_classes = [MultiPartParser, FormParser]

    def get_serializer_class(self):
        if self.action == "create":
            return ImageUploadSerializer
        if self.action == "push":
            return ImagePushSerializer
        return ImageSerializer

    def create(self, request, *args, **kwargs):
        upload = self.get_serializer(data=request.data)
        upload.is_valid(raise_exception=True)
        try:
            image, stats = import_tar(
                upload.validated_data["file"], upload.validated_data["name"], upload.validated_data["tag"]
            )
        except InvalidImage as error:
            raise ValidationError({"file": [str(error)]})
        return Response({"image": ImageSerializer(image).data, "stats": stats}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], parser_classes=[JSONParser])
    def push(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            image = push_image(data["name"], data["tag"], data["files"])
        except MissingBlobs as error:
            return Response({"detail": str(error), "missing": error.digests}, status=status.HTTP_400_BAD_REQUEST)
        except InvalidImage as error:
            raise ValidationError({"files": [str(error)]})
        return Response({"image": ImageSerializer(image).data}, status=status.HTTP_201_CREATED)

    @action(detail=True)
    def index(self, request, pk=None):
        image = self.get_object()
        files = image.files.values_list("path", "blob_id", "blob__size")
        return Response(
            {
                "image": ImageSerializer(image).data,
                "files": [{"path": path, "digest": digest, "size": size} for path, digest, size in files],
            }
        )


@api_view(["POST"])
def missing_blobs(request):
    serializer = MissingBlobsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    wanted = list(dict.fromkeys(serializer.validated_data["digests"]))  # drop repeats, keep order
    stored = Blob.objects.in_bulk(wanted)
    return Response({"missing": [digest for digest in wanted if digest not in stored]})


@csrf_exempt  # nothing here relies on a login cookie, and API clients don't send CSRF tokens
@require_http_methods(["GET", "HEAD", "PUT"])
def blob(request, digest):
    if not storage.DIGEST_RE.fullmatch(digest):
        return JsonResponse({"detail": "Not a sha256 checksum."}, status=404)
    if request.method == "PUT":
        return upload_blob(request, digest)
    return download_blob(digest)


def download_blob(digest):
    if not Blob.objects.filter(digest=digest).exists():
        return JsonResponse({"detail": "No blob with this checksum."}, status=404)
    response = FileResponse(storage.open_blob(digest), content_type="application/octet-stream")
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    response["ETag"] = f'"{digest}"'
    return response


def upload_blob(request, digest):
    existing = Blob.objects.filter(digest=digest).first()
    if existing:
        return JsonResponse({"digest": digest, "size": existing.size})

    length = request.headers.get("Content-Length", "")
    if not length.isdigit():
        return JsonResponse({"detail": "Content-Length is required."}, status=411)
    if int(length) > settings.IMAGE_MAX_SIZE:
        return JsonResponse({"detail": "The blob is larger than the image size limit."}, status=413)

    try:
        _, size = storage.save_blob(request, expected_digest=digest)
    except storage.DigestMismatch:
        return JsonResponse({"detail": "The uploaded bytes don't match the checksum in the URL."}, status=400)
    Blob.objects.get_or_create(digest=digest, defaults={"size": size})
    return JsonResponse({"digest": digest, "size": size}, status=201)


@api_view(["GET"])
def stats(request):
    image_bytes = Image.objects.aggregate(total=Sum("total_size"))["total"] or 0
    used_blobs = Blob.objects.filter(digest__in=ImageFile.objects.values("blob"))
    stored = used_blobs.aggregate(count=Count("digest"), total=Sum("size"))
    stored_bytes = stored["total"] or 0
    return Response(
        {
            "images": Image.objects.count(),
            "unique_files_stored": stored["count"],
            "image_bytes": image_bytes,  # what storing every image in full would take
            "stored_bytes": stored_bytes,  # what is actually stored
            "saved_bytes": image_bytes - stored_bytes,
        }
    )
