"""Every statutory figure carries a citation — enforced, not trusted.

Generated from the model registry, like the tenant isolation suite, because the
specific failure this guards against is silent. Django does **not** merge an
abstract base's ``Meta.constraints`` into a child that declares its own ``Meta``,
and every model here declares one for ``db_table``. Put the CHECK on
``CitedStatutoryModel.Meta`` and it simply does not exist in the database, while
the base class makes the code read as though it does.

That is the same shape as row-level security without FORCE: protection that is
convincing in the source and absent at runtime. So the constraints are added
explicitly per model, and this suite asserts they actually landed.
"""

from __future__ import annotations

import pytest
from django.apps import apps
from django.db import connection

from statutory.models import CitedStatutoryModel, EffectiveDatedModel


def concrete(base):
    return [m for m in apps.get_models() if issubclass(m, base) and not m._meta.abstract]


def ids(models_):
    return [f"{m._meta.app_label}.{m.__name__}" for m in models_]


CITED = concrete(CitedStatutoryModel)
DATED = concrete(EffectiveDatedModel)


def check_constraints_on(table: str) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = %s::regclass AND contype = 'c'
            """,
            [table],
        )
        return {row[0] for row in cursor.fetchall()}


@pytest.mark.statutory
def test_discovery_found_cited_models():
    assert CITED, "No CitedStatutoryModel subclasses found - discovery is broken."


@pytest.mark.statutory
@pytest.mark.parametrize("model", CITED, ids=ids(CITED))
def test_a_blank_citation_is_refused_by_the_database(db, model):
    table = model._meta.db_table
    expected = f"{table}_source_reference_not_blank"
    assert expected in check_constraints_on(table), (
        f"{model.__name__} inherits CitedStatutoryModel but {table} has no CHECK "
        f"forbidding a blank source_reference. Add "
        f"source_reference_not_blank('{table}') to its Meta.constraints - it is NOT "
        f"inherited from the abstract base."
    )


@pytest.mark.statutory
@pytest.mark.parametrize("model", DATED, ids=ids(DATED))
def test_an_inverted_effective_range_is_refused_by_the_database(db, model):
    table = model._meta.db_table
    expected = f"{table}_effective_range_ordered"
    assert expected in check_constraints_on(table), (
        f"{model.__name__} is effective-dated but {table} has no CHECK that "
        f"effective_to is after effective_from. Add effective_range_ordered('{table}')."
    )
