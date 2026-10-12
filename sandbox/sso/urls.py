from pathlib import Path

import django
from django.contrib import admin
from django.urls import path, re_path
from django.views.static import serve

admin.site.site_header = "Acme SSO"
# the admin's own icons (images only; its stylesheets aren't served, see skin.py)
ADMIN_IMG = Path(django.__file__).parent / "contrib" / "admin" / "static" / "admin" / "img"
urlpatterns = [path("admin/", admin.site.urls),
               re_path(r"^static/admin/img/(?P<path>.+)$", serve, {"document_root": str(ADMIN_IMG)})]
