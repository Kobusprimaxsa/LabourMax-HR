from django.apps import AppConfig


class EmployersConfig(AppConfig):
    """Employer entity, statutory registrations, workplaces, pay groups, payroll components."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "employers"
