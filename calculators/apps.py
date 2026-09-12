from django.apps import AppConfig


class CalculatorsConfig(AppConfig):
    """Pure calculation functions. NO ORM, NO I/O, NO clock reads."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "calculators"
