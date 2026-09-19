"""The calculators rule, as a test rather than as discipline (D-207).

CLAUDE.md: "``calculators/`` contains pure functions only. No ORM imports. No
database access. No file I/O. No ``datetime.now()``." That rule has held so far
because one person kept it. This reads every module under ``calculators/`` and
asserts it — six guards in this build have read correctly and done nothing, and
a rule with no test is the seventh waiting to happen.

Read as SOURCE, never by importing: a module that broke the rule by importing
Django at module level would have to be imported to be checked, and then the
check has run the thing it exists to refuse.

The two scanners are separate functions, tested against violating snippets of
their own. Pointing them at the real modules proves only that today's modules
are clean; feeding them a module that imports ``django.db`` is what proves they
would notice tomorrow's.

``apps.py`` and the other Django scaffolding are named exceptions, exactly as
``statutory/tests/test_no_hardcoded_rates.py`` names them: ``calculators/`` is a
Django app so it sits beside the others in the directory listing, and the purity
rule is about the calculation modules.
"""

from __future__ import annotations

import ast
import pathlib
from decimal import Decimal

import pytest

from calculators.base import Money, StatutoryFigure

CALCULATORS = pathlib.Path(__file__).resolve().parents[1]

#: The whole of what a calculation module may import. Anything else is state,
#: I/O or a clock, and all three are what make a calculation unrepeatable.
ALLOWED_ROOTS = frozenset(
    {
        "decimal",
        "dataclasses",
        "datetime",
        "typing",
        "enum",
        "collections",
        "__future__",
        "calculators",
    }
)

#: Matched against the END of the dotted call path, so ``datetime.now()``,
#: ``datetime.datetime.now()`` and ``dt.datetime.utcnow()`` are all caught. The
#: first version checked a single-level name only and missed the second, which
#: is the spelling a calculator would actually use.
FORBIDDEN_SUFFIXES = (
    "datetime.now",
    "datetime.today",
    "datetime.utcnow",
    "date.today",
    "time.time",
    "time.monotonic",
)

SCAFFOLDING = {"apps.py", "models.py", "__init__.py", "admin.py"}


def calculation_modules() -> list[pathlib.Path]:
    return sorted(
        path
        for path in CALCULATORS.rglob("*.py")
        if path.name not in SCAFFOLDING
        and "tests" not in path.parts
        and "migrations" not in path.parts
        and "__pycache__" not in path.parts
    )


def impure_imports(source: str) -> list[str]:
    """Every import in ``source`` that a pure calculator may not make."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        offenders += [
            f"line {node.lineno}: {name}"
            for name in names
            if name.split(".")[0] not in ALLOWED_ROOTS
        ]
    return offenders


def clock_calls(source: str) -> list[str]:
    """Every "what time is it" call in ``source``, however it is spelled."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        path, target = [node.func.attr], node.func.value
        while isinstance(target, ast.Attribute):
            path.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            path.append(target.id)
        dotted = ".".join(reversed(path))
        if any(dotted.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            offenders.append(f"line {node.lineno}: {dotted}()")
    return offenders


WHY_PURE = (
    "A calculator takes its statutory figures, its dates and its settings as INPUTS. "
    "Reaching for the ORM, the filesystem or a clock is what makes a March 2026 payslip "
    "re-run differently in 2029."
)


# ------------------------------------------------- the scanners find violations


def test_the_import_scanner_finds_the_orm_and_the_filesystem():
    source = "\n".join(
        [
            "import datetime",
            "from django.db import models",
            "import os",
            "from calculators.base import Money",
        ]
    )
    assert impure_imports(source) == ["line 2: django.db", "line 3: os"]
    assert impure_imports("from decimal import Decimal\nimport enum") == []


@pytest.mark.parametrize(
    "call",
    ["datetime.now()", "datetime.datetime.now()", "dt.datetime.utcnow()", "date.today()"],
)
def test_the_clock_scanner_finds_every_spelling(call):
    assert clock_calls(f"def f():\n    return {call}\n"), call


def test_the_clock_scanner_leaves_ordinary_calls_alone():
    assert clock_calls("def f(a, b):\n    return a.quantize(b).normalize()\n") == []


# ------------------------------------------------ and the real modules are pure


def test_there_are_calculation_modules_to_check():
    """If the layout changes, the guards below must not quietly pass over
    nothing at all."""
    names = {path.name for path in calculation_modules()}
    assert {"base.py", "attendance.py", "uif.py", "sdl.py"} <= names, f"found only {sorted(names)}"


@pytest.mark.parametrize("module", calculation_modules(), ids=lambda p: p.name)
def test_a_calculator_imports_nothing_that_could_make_it_impure(module):
    offenders = impure_imports(module.read_text(encoding="utf-8"))
    assert not offenders, (
        f"calculators/{module.name} imports something a pure function may not: "
        f"{offenders}. {WHY_PURE} Allowed: {', '.join(sorted(ALLOWED_ROOTS))}"
    )


@pytest.mark.parametrize("module", calculation_modules(), ids=lambda p: p.name)
def test_a_calculator_never_asks_what_time_it_is(module):
    """The import guard allows ``datetime`` for its types. This stops the one use
    of it that breaks replay."""
    offenders = clock_calls(module.read_text(encoding="utf-8"))
    assert not offenders, (
        f"calculators/{module.name} reads the clock: {offenders}. Every date a calculator "
        f"needs arrives as an input, including the date the calculation is FOR."
    )


# --------------------------------------------------------- the shared contract


def test_a_statutory_figure_refuses_a_float():
    """Invariant 6: money is Decimal everywhere. A float reaching a
    StatutoryFigure would have rounded before any calculation ran."""
    with pytest.raises(TypeError, match="not Decimal"):
        StatutoryFigure(value=0.01, table="statutory_parameter", row_id=1)


def test_money_carries_both_figures_and_prints_the_rounded_one():
    money = Money.of(Decimal("12.345"))
    assert money.exact == Decimal("12.345000")
    assert money.rounded == Decimal("12.35")
    assert str(money) == "12.35"
