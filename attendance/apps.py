from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    """Daily capture, import, timesheet aggregation."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "attendance"

    def ready(self):
        # Connected here rather than at import time so the model registry is
        # fully populated first — the same reason core.audit's signals wait
        # for CoreConfig.ready() (D-153).
        from attendance.staleness import connect_signals

        connect_signals()
