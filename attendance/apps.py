from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    """Daily capture, import, timesheet aggregation."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "attendance"
