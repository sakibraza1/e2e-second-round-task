from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("images", views.ImageViewSet, basename="image")

urlpatterns = [
    path("", include(router.urls)),
    path("blobs/missing/", views.missing_blobs, name="missing-blobs"),  # must come before blobs/<digest>/
    path("blobs/<str:digest>/", views.blob, name="blob"),
    path("stats/", views.stats, name="stats"),
]
