"""Acme SSO: the company's identity directory, run on *stock Django admin* (django.contrib.auth).

This is the one app in the sandbox whose UI the author of Rung did not write: the
agent has to operate Django's own admin pages (classic HTML form posts, two-step
user creation, no JSON API) with a non-superuser staff account.
"""
from sandbox.common import SANDBOX

SECRET_KEY = "sandbox-not-secret"
DEBUG = False
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = ["django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
                  "django.contrib.sessions", "django.contrib.messages"]
MIDDLEWARE = ["django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware",
              "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware",
              "django.contrib.messages.middleware.MessageMiddleware"]
ROOT_URLCONF = "sandbox.sso.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True,
              "OPTIONS": {"context_processors": ["django.template.context_processors.request",
                                                 "django.contrib.auth.context_processors.auth",
                                                 "django.contrib.messages.context_processors.messages"]}}]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(SANDBOX / "sso.db")}}
TIME_ZONE = "Asia/Kolkata"
USE_TZ = True
STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
