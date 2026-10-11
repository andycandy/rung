from django.contrib import admin
from django.urls import path

admin.site.site_header = "Acme SSO"
urlpatterns = [path("admin/", admin.site.urls)]
