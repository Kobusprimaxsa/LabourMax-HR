"""``python manage.py generateemployeeimporttemplate`` — the bulk import .xlsx.

Writes the workbook ``employees/importing.py::build_template_workbook`` builds
from ``EXPECTED_COLUMNS`` — the same spec ``parse_workbook`` reads. There is no
second copy of the column list to drift from it (D-122).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from employees.importing import build_template_workbook

DEFAULT_OUTPUT = "employee_import_template.xlsx"


class Command(BaseCommand):
    help = "Write the employee bulk import .xlsx template, generated from the live column spec."

    def add_arguments(self, parser):
        parser.add_argument(
            "output",
            nargs="?",
            default=DEFAULT_OUTPUT,
            help=f"Path to write the template to (default: {DEFAULT_OUTPUT}).",
        )

    def handle(self, *args, **options):
        workbook = build_template_workbook()
        output = options["output"]
        workbook.save(output)
        self.stdout.write(self.style.SUCCESS(f"Wrote {output}"))
