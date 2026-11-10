from django.urls import path

from catalog import views

app_name = "catalog"

urlpatterns = [
    path("", views.home, name="home"),
    path("subscriptions/", views.subscriptions, name="subscriptions"),
    path("subscriptions/data/", views.subscriptions_data, name="subscriptions_data"),
    path("body/", views.health, name="health"),
    path("body/data/", views.health_data, name="health_data"),
    path("body/coverage/", views.health_coverage, name="health_coverage"),
    path("body/workouts/<int:pk>/", views.workout, name="workout"),
    path("body/workouts/<int:pk>/data/", views.workout_data, name="workout_data"),
]
