from django.urls import path

from . import views

urlpatterns = [
    path("", views.ingest),
    path("status/", views.status),
]
