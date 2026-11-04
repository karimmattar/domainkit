"""The three routes."""

from django.urls import path

from . import views

urlpatterns = [
    path("items", views.items),
    path("items/<uuid:item_id>", views.read),
]
