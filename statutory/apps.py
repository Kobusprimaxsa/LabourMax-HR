from django.apps import AppConfig


class StatutoryConfig(AppConfig):
    """Effective-dated statutory reference data and the loader."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "statutory"
