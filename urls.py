from django.urls import path

from .views import courses_all

urlpatterns = [
    path("all", courses_all, name="course_catalog_cache_courses_all"),
]
