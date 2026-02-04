from django.urls import path
from .views import courses_all, courses_refresh

urlpatterns = [
    path("all", courses_all, name="course_catalog_cache_courses_all"),
    path("refresh", courses_refresh, name="course_catalog_cache_courses_refresh"),
]
