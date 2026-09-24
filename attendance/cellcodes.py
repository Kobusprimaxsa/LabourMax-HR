"""What a person types into a grid cell, and what a cell shows (D-299).

The ten-minute target lives or dies here: every ordinary day must be one or two
keystrokes. So a cell takes a short code rather than a form:

=========  ==========================================================
``9``      hours worked — ordinary, or Sunday or public holiday hours when
           the date is one (the date says which, not the typist)
``8.5``    decimal hours; a comma works too (``8,5``)
``O``      an ordinary day at the employee's scheduled times
``O9``     an ordinary day, nine hours
``S4``     four hours on a Sunday
``H``      a public holiday not worked (paid under s18(1))
``H6``     six hours worked on a public holiday
``B8``     eight hours of standby
``R``      rest day            ``A``  absent, unpaid
``P``      absent, paid        ``N``  no work available
=========  ==========================================================

``L`` is refused: a leave day is written by an approved leave application
(P6), never typed. An empty cell is refused too — there is no service that
deletes a captured day, and blanking a cell must not look as though it did.

**Every day type has a LETTER as well as a colour**, so the grid never relies on
colour alone (the brief's accessibility rule): the letter is what the cell
shows, and the colour is only the second signal.

Pure: no database, so every rule here is tested without one. The caller says
whether the date is a public holiday and what the schedule says.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from decimal import Decimal, InvalidOperation

from attendance.models import AttendanceDay

DayType = AttendanceDay.DayType

LETTERS = {
    DayType.ORDINARY: "O",
    DayType.REST_DAY: "R",
    DayType.SUNDAY: "S",
    DayType.PUBLIC_HOLIDAY: "H",
    DayType.LEAVE: "L",
    DayType.ABSENT_UNPAID: "A",
    DayType.ABSENT_PAID: "P",
    DayType.NO_WORK_AVAILABLE: "N",
}
STANDBY = "B"
BY_LETTER = {letter: day_type for day_type, letter in LETTERS.items()}

#: Day types on which hours are never worked.
NO_HOURS = {DayType.REST_DAY, DayType.ABSENT_UNPAID, DayType.ABSENT_PAID, DayType.NO_WORK_AVAILABLE}
MAX_HOURS = Decimal("24")

_CODE = re.compile(r"^\s*([A-Za-z])?\s*(\d{1,2}(?:[.,]\d{1,2})?)?\s*$")


class CellCodeError(ValueError):
    """What was typed cannot be saved. The message is shown on the cell."""


@dataclasses.dataclass(frozen=True)
class CellEntry:
    day_type: str
    hours: Decimal | None = None
    is_standby: bool = False
    #: ``O`` with no hours: capture at the schedule's own times.
    use_schedule: bool = False


def parse(text: str, *, work_date: datetime.date, is_public_holiday: bool) -> CellEntry:
    """What was typed, as the values ``capture()`` takes — or a refusal."""
    match = _CODE.match(text or "")
    if not text or not text.strip():
        raise CellCodeError("Empty. Nothing was saved — a captured day is not deleted from here.")
    if match is None:
        raise CellCodeError(f"'{text.strip()}' is not a code. Type hours (9) or a letter (R, A…).")
    letter, number = match.group(1), match.group(2)
    letter = letter.upper() if letter else None

    hours = None
    if number is not None:
        try:
            hours = Decimal(number.replace(",", "."))
        except InvalidOperation as error:  # pragma: no cover - the regex admits only digits
            raise CellCodeError(f"'{number}' is not a number of hours.") from error
        if not Decimal("0") < hours <= MAX_HOURS:
            raise CellCodeError("Hours must be more than 0 and no more than 24.")

    if letter is None:
        return CellEntry(day_type=_inferred(work_date, is_public_holiday), hours=hours)
    if letter == STANDBY:
        if hours is None:
            raise CellCodeError("Standby needs the hours: B8.")
        return CellEntry(
            day_type=_inferred(work_date, is_public_holiday), hours=hours, is_standby=True
        )
    if letter == "L":
        raise CellCodeError("Leave is written by an approved leave application, not typed here.")
    if letter not in BY_LETTER:
        raise CellCodeError(f"'{letter}' is not a day type. Use O, R, S, H, A, P, N or B.")

    day_type = BY_LETTER[letter]
    if day_type in NO_HOURS and hours is not None:
        raise CellCodeError(f"{day_type.label} takes no hours — type {letter} alone.")
    if day_type == DayType.SUNDAY and hours is None:
        raise CellCodeError("A Sunday worked needs its hours: S4.")
    if day_type == DayType.ORDINARY and hours is None:
        return CellEntry(day_type=day_type, use_schedule=True)
    return CellEntry(day_type=day_type, hours=hours)


def _inferred(work_date: datetime.date, is_public_holiday: bool) -> str:
    """Bare hours take their type from the DATE. A Sunday that is also a public
    holiday is O-40's unread question, and is captured as the public holiday
    — the capture warns and the payroll assembly refuses it (D-291)."""
    if is_public_holiday:
        return DayType.PUBLIC_HOLIDAY
    if work_date.weekday() == 6:
        return DayType.SUNDAY
    return DayType.ORDINARY


def worked_hours(day: AttendanceDay) -> Decimal:
    return (
        day.ordinary_hours
        + day.overtime_hours
        + day.sunday_hours
        + day.public_holiday_hours
        + day.standby_hours_worked
    )


def shown(day: AttendanceDay) -> str:
    """What a captured cell shows, which is also what re-typing it would save:
    the letter, then the hours worked where there are any."""
    letter = STANDBY if day.is_standby else LETTERS.get(day.day_type, "?")
    hours = worked_hours(day)
    if not hours:
        return letter
    text = f"{hours.normalize():f}"
    return text if letter in ("O",) else f"{letter}{text}"
