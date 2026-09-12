from django.apps import AppConfig


class LeaveConfig(AppConfig):
    """Leave types, cycles, the transaction ledger, applications, accrual engine."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "leave"
