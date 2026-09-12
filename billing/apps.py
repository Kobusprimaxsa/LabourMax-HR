from django.apps import AppConfig


class BillingConfig(AppConfig):
    """Plans, price bands, subscriptions, service agreements, invoices, gateway integration."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "billing"
