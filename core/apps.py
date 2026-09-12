from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Tenancy, users, roles, audit, files. Base models every other app inherits."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # Audit signals are connected here rather than at import time so the
        # model registry is fully populated. Connected without a sender, so a
        # model in a later phase is audited the day it inherits AuditedModel.
        from core.audit import connect_signals

        connect_signals()
