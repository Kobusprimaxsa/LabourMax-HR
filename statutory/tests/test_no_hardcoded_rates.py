"""P2's definition of done, as a test rather than as a grep somebody remembers to run.

    "Every statutory number lives in a table with a gazette citation, and ``grep -r``
    finds no hard-coded rate anywhere in the codebase."

A grep is a thing a person does once, on the day the phase closes. What actually
happens afterwards is that someone needs the UIF rate in a hurry, writes
``Decimal("0.01")`` with a ``# TODO: move to statutory`` beside it, and it ships. Two
years later the rate has changed, the table has been updated, and one calculation
still uses the old figure — which is precisely the failure the whole phase was built
to make impossible.

So the grep runs on every commit, and it is written in terms of shape rather than of
particular numbers: nothing is known about which figures are statutory, only that a
decimal constant in application code is either a rounding quantum or a statutory
figure, and there is a very short list of the former.

The scan deliberately excludes tests and migrations. Tests need literal values to
assert against, and a migration that creates a *column* may legitimately mention
precision. A migration that sets a *default* is the case this rule most wants to
catch, and that is covered from the other side by
``test_rule_sets.test_no_rule_set_column_carries_a_default``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

APPLICATION_PACKAGES = [
    "core",
    "billing",
    "statutory",
    "employers",
    "employees",
    "attendance",
    "leave",
    "payroll",
    "calculators",
    "statutory_out",
    "discipline",
    "documents",
    "selfservice",
    "console",
    "reporting",
    "labourmax",
]

EXCLUDED_DIRECTORIES = {"migrations", "tests", "__pycache__", ".venv"}

# `calculators/` is a Django app only so that it sits alongside the others in the
# directory listing. Its AppConfig has to import django.apps, and that is the entire
# extent of the framework's presence in the package — the purity rule is about the
# calculation modules, so the scaffolding is named here rather than left to weaken
# the rule by exception.
DJANGO_SCAFFOLDING = {"apps.py", "admin.py", "models.py", "__init__.py"}

# The only decimal constants that are not statutory figures.
#
# "0.01" is the rounding quantum from invariant 6 — rounding to two places happens at
# the payslip line, and it needs a literal to quantize against. "0.001" is the same
# thing one place further out, for the three-decimal columns like
# pay_period.working_days_in_period. The rest are identity and zero values, and "100"
# converts a percentage to a fraction, which is arithmetic rather than a rate.
#
# Nothing whose VALUE could be a statutory figure belongs here. A quantum is a unit of
# rounding; a rate is a number somebody gazetted.
PERMITTED_DECIMAL_CONSTANTS = {
    "0",
    "0.00",
    "0.0000",
    "0.001",
    "0.01",
    "1",
    "1.00",
    "100",
    "100.00",
    "-1",
}


def application_sources() -> list[pathlib.Path]:
    found = []
    for package in APPLICATION_PACKAGES:
        for path in (REPO_ROOT / package).rglob("*.py"):
            if EXCLUDED_DIRECTORIES & set(path.parts):
                continue
            found.append(path)
    return sorted(found)


def relative(path: pathlib.Path) -> str:
    return str(path.relative_to(REPO_ROOT)).replace("\\", "/")


def test_the_scan_actually_finds_source_files():
    """Guards every test below against silently scanning nothing.

    A path assumption that stops matching turns this whole file into a suite that
    passes by examining zero files, which is the quietest possible failure.
    """
    sources = application_sources()
    assert len(sources) > 20
    assert any(relative(p) == "statutory/models.py" for p in sources)


@pytest.mark.statutory
def test_no_decimal_constant_appears_in_application_code():
    """A ``Decimal("...")`` in application code is a statutory figure or a quantum."""
    offences = []
    for path in application_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "Decimal" or not node.args:
                continue
            argument = node.args[0]
            if not isinstance(argument, ast.Constant):
                continue
            value = str(argument.value)
            if value not in PERMITTED_DECIMAL_CONSTANTS:
                offences.append(f"{relative(path)}:{node.lineno}: Decimal({value!r})")

    assert not offences, (
        "Decimal constants found in application code:\n  "
        + "\n  ".join(offences)
        + "\n\nEvery statutory figure lives in an effective-dated table with a citation "
        "and is read through statutory.resolve. If this is genuinely not a statutory "
        "figure, add it to PERMITTED_DECIMAL_CONSTANTS with a reason."
    )


@pytest.mark.statutory
def test_no_float_literal_appears_in_application_code():
    """Invariant 6: no floating point anywhere, ever.

    A rate written as ``0.01`` is not 0.01, and UIF at one percent of a ceiling is a
    figure employees check by hand against a payslip.
    """
    offences = []
    for path in application_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offences.append(f"{relative(path)}:{node.lineno}: {node.value}")

    assert not offences, (
        "Float literals found in application code:\n  "
        + "\n  ".join(offences)
        + "\n\nMoney and rates are Decimal. If this genuinely is not money, it still "
        "should not be a float - see invariant 6 in CLAUDE.md."
    )


@pytest.mark.statutory
def test_calculators_stay_pure():
    """The calculators rule, enforced before there is anything in the package.

    Pure functions only: no ORM, no I/O, no clock. The moment a calculator can read
    the database it can read *today's* rates, and a re-run of a 2026 payroll in 2030
    stops reproducing 2026.

    Written now, while ``calculators/`` is empty, because a rule added after the
    first violation is a refactor rather than a rule.
    """
    forbidden_modules = ("django", "statutory.models", "psycopg", "requests", "pathlib", "os")
    offences = []

    for path in (REPO_ROOT / "calculators").rglob("*.py"):
        if EXCLUDED_DIRECTORIES & set(path.parts) or path.name in DJANGO_SCAFFOLDING:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for module in names:
                if any(module == bad or module.startswith(bad + ".") for bad in forbidden_modules):
                    offences.append(f"{relative(path)}:{node.lineno}: imports {module}")

    assert not offences, (
        "calculators/ contains pure functions only - no ORM, no I/O, no clock:\n  "
        + "\n  ".join(offences)
    )


@pytest.mark.statutory
def test_calculators_never_read_the_clock():
    """``datetime.now()`` in a calculator makes yesterday's payslip unreproducible."""
    offences = []
    for path in (REPO_ROOT / "calculators").rglob("*.py"):
        if EXCLUDED_DIRECTORIES & set(path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"now", "today", "localdate", "localtime"}:
                    offences.append(f"{relative(path)}:{node.lineno}: {node.func.attr}()")

    assert not offences, "calculators/ must be handed every date it uses:\n  " + "\n  ".join(
        offences
    )
