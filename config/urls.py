from django.contrib import admin
from django.urls import path

from deploy.health import readiness, liveness

urlpatterns = [path("admin/", admin.site.urls),
               path("health/ready/", readiness), path("health/live/", liveness)]
