"""Local development. Never used in production."""

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, INSTALLED_APPS, MIDDLEWARE  # noqa: F401

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

INTERNAL_IPS = ["127.0.0.1"]

# Fixtures only. NEVER point local development at a copy of production data.
