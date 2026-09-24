"""The attendance screens. Every id in these paths is a public_uid or a number
within the month in the path — never an integer primary key."""

from django.urls import path

from attendance import views

app_name = "attendance"

MONTH = "<uuid:employer_uid>/<uuid:group_uid>/<str:month>"

urlpatterns = [
    path(f"{MONTH}/", views.grid, name="grid"),
    path(f"{MONTH}/exceptions/", views.exceptions, name="exceptions"),
    path(f"{MONTH}/bulk/", views.bulk, name="bulk"),
    path(f"{MONTH}/approve/", views.approve, name="approve"),
    path(f"{MONTH}/cell/<uuid:employee_uid>/<int:day>/", views.cell, name="cell"),
]
