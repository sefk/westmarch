from django.urls import path

from catalog import views

app_name = "catalog"

urlpatterns = [
    path("", views.home, name="home"),
    path("subscriptions/", views.subscriptions, name="subscriptions"),
    path("subscriptions/data/", views.subscriptions_data, name="subscriptions_data"),
]
