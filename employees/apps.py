from django.apps import AppConfig


class EmployeesConfig(AppConfig):
    """Employee master file, engagements, remuneration, schedules."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "employees"
