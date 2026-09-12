from django.apps import AppConfig


class PayrollConfig(AppConfig):
    """Pay periods, runs, payslips, YTD accumulators, terminations."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "payroll"
