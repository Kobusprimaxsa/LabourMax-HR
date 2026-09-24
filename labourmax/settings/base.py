"""Settings shared by every environment.

Nothing environment-specific belongs here. See dev.py and prod.py.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-key-do-not-use-in-production")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
]

THIRD_PARTY_APPS = [
    "rest_framework",
]

# Order matters only for template/static resolution. Domain apps map 1:1 onto the
# twelve schema domains — see CLAUDE.md.
LOCAL_APPS = [
    "core",
    "billing",
    "statutory",
    "employers",
    "employees",
    "attendance",
    "leave",
    "payroll",
    "calculators",
    "statutory_out",
    "discipline",
    "documents",
    "selfservice",
    "console",
    "reporting",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Resolves the tenant and pins it for the request AND the database session.
    # Must come after AuthenticationMiddleware.
    "core.middleware.TenantContextMiddleware",
    # Records who is acting, for the audit trail. Must come after
    # AuthenticationMiddleware; without it every change is attributed to "system".
    "core.middleware.AuditContextMiddleware",
]

# Trust X-Forwarded-For for the client IP written into the audit trail. Keep this
# False unless the deployment sits behind a proxy that OVERWRITES the header - a
# client can otherwise choose what the audit trail records about itself.
USE_X_FORWARDED_FOR = False

ROOT_URLCONF = "labourmax.urls"
WSGI_APPLICATION = "labourmax.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.shell",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", default="labourmax"),
        "USER": env("POSTGRES_USER", default="labourmax"),
        "PASSWORD": env("POSTGRES_PASSWORD", default=""),
        "HOST": env("POSTGRES_HOST", default="localhost"),
        "PORT": env("POSTGRES_PORT", default="5432"),
        "ATOMIC_REQUESTS": True,
    }
}

AUTH_USER_MODEL = "core.AppUser"

# Argon2id first — see CLAUDE.md.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-za"
TIME_ZONE = "Africa/Johannesburg"
USE_I18N = True
USE_TZ = True  # everything stored UTC, rendered SAST

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Our own CSS and JS, and HTMX vendored at a pinned version (static/vendor/) -
# never loaded from a CDN, so a screen cannot change because somebody else's
# server did.
STATICFILES_DIRS = [BASE_DIR / "static"]

# ---------------------------------------------------------------- sessions
# D-296. HttpOnly: no script reads the session cookie, ours included. SameSite
# Lax: the cookie is not sent on a cross-site POST, so a forged form on another
# site cannot act as the employer - CSRF is the second lock on the same door.
# The CSRF cookie is HttpOnly too: HTMX sends the token as a header taken from
# the page, not from the cookie. Secure is set in prod.py (dev runs on http).
# Eight hours and ends with the browser: a payroll screen on a shared household
# computer should not still be signed in tomorrow.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

# Failed sign-ins before an account is locked, and for how long. Security
# policy, not a statutory figure: AppUser.failed_login_count and locked_until
# have existed since P0 and nothing set them.
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCKOUT_MINUTES = 15
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Enforced by core.files.store before any bytes are written. Checking after the
# fact means a large upload has already cost the bandwidth and the disk.
FILE_UPLOAD_MAX_BYTES = 20 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Labourmax settings -------------------------------------------------------
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default="")

# Bumped whenever a calculator changes. Stamped onto every payroll_run so a
# historic run can always be reproduced. See CLAUDE.md invariant 5.
PAYROLL_ENGINE_VERSION = "0.1.0"

# Uploads
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

CELERY_BROKER_URL = env("REDIS_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_TIMEZONE = TIME_ZONE

EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.console.EmailBackend")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
