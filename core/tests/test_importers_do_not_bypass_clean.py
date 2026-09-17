"""Validation that only runs in ``clean()`` is validation an importer skips.

The accommodation-deduction ceiling (D-198) is enforced in
``EmployeeRecurringComponent.clean()``, deliberately: the cap resolves per sector
and date with a fallback through ``statutory.resolve``, and a second copy of that
resolver in PL/pgSQL would drift from the first. The justification holds only
while nothing writes those rows around ``clean()``.

This project has two bulk importers (employees, attendance) on the shared
preview-is-apply-rolled-back skeleton in ``core/importing.py``. Both call
``full_clean()`` on the rows they write. The skeleton itself writes no domain
row — each app's own row handler does — so there is no single place to assert it
once; it is asserted here across every importer, as source, so a third importer
inherits the rule instead of quietly not having it.

Two guards, both keyed to what would actually break:

1. every ``save()`` in an importer is preceded by ``full_clean()`` in the same
   function, so a clean()-only rule still runs
2. no importer writes ``EmployeeRecurringComponent`` at all today

Reading source rather than behaviour is the point: an importer that does not yet
exist cannot be exercised, and this must fail on the day it is written.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# Rows that are the import's own bookkeeping, not captured data: the batch record
# and the stored file. Their models carry no clean() rule to bypass.
BOOKKEEPING = {"batch", "file_object", "source_file", "self"}

WHAT_TO_DO = (
    "Either route the importer through full_clean(), or move the rule into a "
    "BEFORE INSERT OR UPDATE trigger. Do not delete this test."
)


def importer_modules() -> list[pathlib.Path]:
    return sorted(
        p for p in REPO.glob("*/importing.py") if ".venv" not in p.parts and "tests" not in p.parts
    )


def test_the_importers_this_test_guards_still_exist():
    """If importing.py is renamed, both guards below would silently pass over
    nothing at all."""
    found = {p.parent.name for p in importer_modules()}
    assert {"core", "employees", "attendance"} <= found, f"found only {found}"


@pytest.mark.parametrize("module", importer_modules(), ids=lambda p: p.parent.name)
def test_every_importer_full_cleans_the_rows_it_writes(module):
    tree = ast.parse(module.read_text(encoding="utf-8"))
    offenders = []
    for function in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        saved, cleaned = set(), set()
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            target = node.func.value
            name = target.id if isinstance(target, ast.Name) else None
            if node.func.attr == "save" and name and name not in BOOKKEEPING:
                saved.add(name)
            if node.func.attr == "full_clean" and name:
                cleaned.add(name)
        for name in sorted(saved - cleaned):
            offenders.append(f"{module.parent.name}/importing.py::{function.name} saves {name}")

    assert not offenders, (
        "An importer writes a row without full_clean(), so every clean()-only rule is "
        "skipped for it — the accommodation ceiling (D-198) among them:\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + f"\n{WHAT_TO_DO}"
    )


def test_no_importer_writes_a_recurring_component():
    """The accommodation ceiling lives on employee_recurring_component, which no
    importer touches today. The day one does, this fails."""
    writers = [
        module.parent.name
        for module in importer_modules()
        if "EmployeeRecurringComponent" in module.read_text(encoding="utf-8")
    ]
    assert not writers, (
        f"A bulk importer now exists for employee_recurring_component ({', '.join(writers)}). "
        "The accommodation-deduction ceiling (D-198) is enforced in clean() only, which an "
        f"importer bypasses. {WHAT_TO_DO}"
    )
