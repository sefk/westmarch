from django.urls import path

from catalog import views

app_name = "catalog"

urlpatterns = [
    path("", views.home, name="home"),
    path("subscriptions/", views.subscriptions, name="subscriptions"),
    path("subscriptions/data/", views.subscriptions_data, name="subscriptions_data"),
    path("subscriptions/local/", views.subscriptions_local, name="subscriptions_local"),
    path("projects/", views.projects, name="projects"),
    path("projects/data/", views.projects_data, name="projects_data"),
    path("projects/<int:pk>/", views.project, name="project"),
    path("body/", views.health, name="health"),
    path("body/data/", views.health_data, name="health_data"),
    path("body/coverage/", views.health_coverage, name="health_coverage"),
    path("body/workouts/", views.workouts, name="workouts"),
    path("body/workouts/data/", views.workouts_data, name="workouts_data"),
    path("body/workouts/<int:pk>/", views.workout, name="workout"),
    path("body/workouts/<int:pk>/data/", views.workout_data, name="workout_data"),
]
