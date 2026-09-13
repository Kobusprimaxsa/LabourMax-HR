"""South African identity numbers — structure, checksum, and what they imply.

Pure functions. No ORM, no I/O, no clock: every date this module reasons about is
handed to it, so a validation run in 2031 against a 2026 capture reaches the same
answer. That is the ``calculators/`` rule applied here because the reasons are the
same, even though this is not a calculator.

**The structure**, thirteen digits, ``YYMMDDSSSSCAZ``:

====== =============================================================
 1-6    Date of birth, ``YYMMDD``. The century is not in the number
 7-10   Sequence within that birth date. 0000-4999 female, 5000-9999 male
 11     Citizenship: 0 South African citizen, 1 permanent resident
 12     Historically a race digit, abolished in 1994. Now unused, usually 8
 13     Luhn check digit
====== =============================================================

**The century problem is real and has no clean answer.** ``900101`` is 1990 or
2090, and the number itself does not say. Every South African payroll system
resolves it with a rule, and every rule is wrong for somebody. This module does
not guess: it returns both candidate dates and lets the caller compare them
against the captured date of birth, which the employer has from a document. That
turns an ambiguity into a cross-check.

**The checksum is worth much more than it looks.** It catches a single mistyped
digit and most transpositions, which is exactly how ID numbers get captured wrong.
The date cross-check catches the rest: a number whose first six digits disagree
with the date of birth on the same form is a capture error in one field or the
other, and neither the checksum nor a human reading them separately would notice.

Validation is deliberately **advisory** for the non-``sa_id`` types. A passport, an
asylum permit and a work permit have no checksum this system can verify, so
``id_number_verified`` stays false for them and means what it says: nobody has
verified this. It does not mean the number is wrong.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

SA_ID_LENGTH = 13

#: Digits 7-10. Below this is recorded female, at or above it male. Kept as a
#: constant because it is a property of the numbering scheme, not a rate.
FEMALE_SEQUENCE_CEILING = 5000


@dataclass(frozen=True)
class IdNumberCheck:
    """What could be established about a number, and what could not.

    ``is_valid`` means the structure and checksum hold. It is not a claim that the
    number belongs to the person — nothing available here can establish that.
    """

    is_valid: bool
    reasons: tuple[str, ...] = ()
    birth_date_candidates: tuple[datetime.date, ...] = ()
    implied_gender: str | None = None
    is_citizen: bool | None = None

    @property
    def failed(self) -> bool:
        return not self.is_valid


def luhn_check_digit(digits: str) -> int:
    """The Luhn check digit for a string of digits, excluding the check digit itself.

    Written out rather than imported because it is nine lines and a dependency for
    nine lines is a dependency to keep patched forever.
    """
    total = 0
    for position, character in enumerate(reversed(digits)):
        value = int(character)
        if position % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return (10 - total % 10) % 10


def birth_date_candidates(yymmdd: str) -> tuple[datetime.date, ...]:
    """Both dates ``YYMMDD`` could mean, earliest first.

    Returns an empty tuple when the six digits are not a date at all — 30 February
    is the case this catches, and it is a common transposition of 03 and 30.
    """
    year, month, day = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
    found = []
    for century in (1900, 2000):
        try:
            found.append(datetime.date(century + year, month, day))
        except ValueError:
            continue
    return tuple(found)


def check_sa_id(number: str) -> IdNumberCheck:
    """Structure and checksum. Says what is wrong, not merely that something is."""
    digits = "".join(character for character in (number or "") if character.isdigit())
    reasons: list[str] = []

    if len(digits) != SA_ID_LENGTH:
        return IdNumberCheck(
            is_valid=False,
            reasons=(
                f"A South African ID number has {SA_ID_LENGTH} digits; this one has {len(digits)}.",
            ),
        )

    candidates = birth_date_candidates(digits[:6])
    if not candidates:
        reasons.append(
            f"The first six digits, {digits[:6]}, are not a date. Check the day and "
            f"month — 30 and 03 transposed is the usual cause."
        )

    if luhn_check_digit(digits[:12]) != int(digits[12]):
        reasons.append(
            "The check digit does not match the rest of the number, so at least one "
            "digit is wrong. This is what a single mistyped digit looks like."
        )

    citizenship = digits[10]
    if citizenship not in {"0", "1"}:
        reasons.append(
            f"The citizenship digit is {citizenship}; it can only be 0 (citizen) or "
            f"1 (permanent resident)."
        )

    sequence = int(digits[6:10])
    return IdNumberCheck(
        is_valid=not reasons,
        reasons=tuple(reasons),
        birth_date_candidates=candidates,
        implied_gender="female" if sequence < FEMALE_SEQUENCE_CEILING else "male",
        is_citizen=citizenship == "0" if citizenship in {"0", "1"} else None,
    )


def birth_date_agrees(number: str, date_of_birth: datetime.date) -> bool:
    """Does the captured date of birth match either century reading of the number?

    The check that earns its place. A checksum catches a mistyped digit inside the
    number; this catches the case where the number is internally perfect and belongs
    to a different person, or where the date of birth was typed from the wrong line
    of the document. Both are captures that look right on the screen.
    """
    if date_of_birth is None:
        return False
    return date_of_birth in birth_date_candidates(
        "".join(c for c in (number or "") if c.isdigit())[:6]
    )


def age_on(date_of_birth: datetime.date, on_date: datetime.date) -> int:
    """Completed years on a given date.

    Takes the date rather than reading the clock, so that an engagement validated at
    capture and re-validated in a later audit reaches the same answer.
    """
    had_birthday = (on_date.month, on_date.day) >= (date_of_birth.month, date_of_birth.day)
    return on_date.year - date_of_birth.year - (0 if had_birthday else 1)
