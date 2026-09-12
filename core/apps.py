from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Tenancy, users, roles, audit, files. Base models every other app inherits."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
