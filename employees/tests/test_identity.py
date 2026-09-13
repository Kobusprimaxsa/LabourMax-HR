"""The South African ID number — the checks, and what each one actually catches.

No database in this file. ``employees/identity.py`` is pure, so its tests are too,
and the cases below are about arithmetic and calendars rather than about rows.

Every valid number here is BUILT from its own check digit rather than typed in, so
the fixtures cannot be quietly wrong and no real person's number appears in the
repository.
"""

from __future__ import annotations

import datetime

import pytest

from employees.identity import (
    SA_ID_LENGTH,
    age_on,
    birth_date_agrees,
    birth_date_candidates,
    check_sa_id,
    luhn_check_digit,
)


def make_id(yymmdd="900101", sequence="5009", citizenship="0", race="8"):
    """A structurally valid number, with its own check digit computed."""
    body = f"{yymmdd}{sequence}{citizenship}{race}"
    return body + str(luhn_check_digit(body))


# -------------------------------------------------------------------- the checksum


def test_a_number_built_from_its_own_check_digit_is_valid():
    check = check_sa_id(make_id())
    assert check.is_valid
    assert check.reasons == ()


@pytest.mark.parametrize("position", range(12))
def test_a_single_mistyped_digit_is_caught(position):
    """The whole reason the check digit exists.

    Every one of the twelve positions, because a checksum that catches eleven of
    them is a checksum with a hole somebody will eventually fall into.
    """
    number = make_id()
    digits = list(number)
    digits[position] = str((int(digits[position]) + 1) % 10)
    assert check_sa_id("".join(digits)).failed


def test_the_usual_transposition_is_caught():
    """Two adjacent digits swapped is the second most common capture error."""
    number = make_id(sequence="5109")
    swapped = number[:6] + number[7] + number[6] + number[8:]
    assert swapped != number
    assert check_sa_id(swapped).failed


def test_the_wrong_length_says_so_rather_than_failing_the_checksum():
    check = check_sa_id("900101500908")
    assert check.failed
    assert str(SA_ID_LENGTH) in check.reasons[0]
    assert "12" in check.reasons[0]


def test_letters_and_spaces_are_stripped_before_checking():
    number = make_id()
    assert check_sa_id(f"{number[:6]} {number[6:10]} {number[10:]}").is_valid


# ---------------------------------------------------------------- the date reading


def test_both_centuries_are_offered_because_the_number_does_not_say():
    """THE AMBIGUITY THAT HAS NO CLEAN ANSWER.

    ``900101`` is 1990 or 2090. Every South African payroll system resolves this
    with a rule and every rule is wrong for somebody, so this module returns both
    and lets the captured date of birth decide.
    """
    assert birth_date_candidates("900101") == (
        datetime.date(1990, 1, 1),
        datetime.date(2090, 1, 1),
    )


def test_a_date_that_exists_in_only_one_century_returns_only_that_one():
    """2000 was a leap year; 1900 was not. The rule catches people out."""
    assert birth_date_candidates("000229") == (datetime.date(2000, 2, 29),)


def test_an_impossible_date_is_named_as_such():
    check = check_sa_id(make_id(yymmdd="900230"))
    assert check.failed
    assert "not a date" in check.reasons[0]
    assert "30 and 03 transposed" in check.reasons[0]


def test_the_date_of_birth_cross_check_accepts_either_century():
    number = make_id()
    assert birth_date_agrees(number, datetime.date(1990, 1, 1))
    assert birth_date_agrees(number, datetime.date(2090, 1, 1))


def test_a_date_of_birth_from_a_different_person_is_caught():
    """THE CHECK THAT EARNS ITS PLACE.

    This number's checksum is perfect. It simply is not this employee's number —
    or the date of birth was typed from the wrong line of the document. Nothing
    that validates either field on its own can see it, and the error reaches SARS.
    """
    number = make_id(yymmdd="900101")
    assert not birth_date_agrees(number, datetime.date(1990, 1, 2))


def test_a_missing_date_of_birth_does_not_pass_by_default():
    assert not birth_date_agrees(make_id(), None)


# -------------------------------------------------------------- what else it says


def test_the_sequence_digits_imply_a_gender():
    assert check_sa_id(make_id(sequence="4999")).implied_gender == "female"
    assert check_sa_id(make_id(sequence="5000")).implied_gender == "male"


def test_citizenship_is_read_and_anything_else_is_refused():
    assert check_sa_id(make_id(citizenship="0")).is_citizen is True
    assert check_sa_id(make_id(citizenship="1")).is_citizen is False

    check = check_sa_id(make_id(citizenship="7"))
    assert check.failed
    assert "citizenship digit" in " ".join(check.reasons)


# ------------------------------------------------------------------------- age


def test_age_is_completed_years():
    born = datetime.date(2000, 6, 15)
    assert age_on(born, datetime.date(2026, 6, 14)) == 25
    assert age_on(born, datetime.date(2026, 6, 15)) == 26
    assert age_on(born, datetime.date(2026, 6, 16)) == 26


def test_a_birthday_on_29_february_still_ages_in_a_common_year():
    """Somebody born on 29 February 2000 is 26 on 1 March 2026, not 25."""
    born = datetime.date(2000, 2, 29)
    assert age_on(born, datetime.date(2026, 2, 28)) == 25
    assert age_on(born, datetime.date(2026, 3, 1)) == 26
