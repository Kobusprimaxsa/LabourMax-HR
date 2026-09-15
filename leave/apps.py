from django.apps import AppConfig


class LeaveConfig(AppConfig):
    """Leave types, cycles, the transaction ledger, applications, accrual engine."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "leave"

    def ready(self):
        # Connected here rather than at import time so the model registry is
        # fully populated first — the same reason attendance's own staleness
        # signal waits for AttendanceConfig.ready() (D-153).
        from leave.staleness import connect_signals

        connect_signals()
