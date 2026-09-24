"""``--golden-tests-passed`` names a command, and the command selects a known set.

Before D-283 both ``verifystatutory`` and ``importverification`` accepted the
flag with help text reading "The published worked examples reproduce", and
nobody could say which tests that meant. Three files transcribing the BCEA
carried the ``golden`` mark beside the SARS reproductions, so the answer
depended on who was asked. A verifier was being asked to assert something
undefined, which is a guard that reads convincingly and does nothing.

Now: ``statutory.verification.GOLDEN_COMMAND`` is the command, both help texts
print it, and ``GOLDEN_MODULES`` below is every file the command selects. A new
file that adds the mark fails here until somebody adds it to the list and to
the table in CLAUDE.md — which is the moment to ask whether it reproduces a
PUBLISHED figure or only the words of a section (``statute``).
"""

from __future__ import annotations

import configparser
import pathlib
import re

import pytest

from statutory.management.commands.importverification import Command as ImportCommand
from statutory.management.commands.verifystatutory import Command as VerifyCommand
from statutory.verification import GOLDEN_COMMAND

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Every module ``pytest -m golden`` collects from, and the published source
#: each reproduces. Keep in step with CLAUDE.md's "The golden set" table.
GOLDEN_MODULES = {
    "calculators/tests/test_paye_golden.py": "SARS PAYE-GEN-01-G01 rev 16 §4; G20 rev 0",
    "calculators/tests/test_paye_golden_2027.py": "SARS PAYE-GEN-01-G01 rev 16 §6; G21 rev 1",
    "calculators/tests/test_uif.py": "SARS, 'UIF ceiling earnings', 3 Aug 2021 (R177,12)",
    "calculators/tests/test_sdl.py": "SDL Act s3(1)(a)(ii), SDL-GEN-01-G01 §6 (the rate)",
    "calculators/tests/test_coida.py": "DEL GN 2390 of 2024 p5, GN 1723 of 2023 (capping)",
    "statutory/tests/test_golden_figures.py": "the golden literals against ref-2026.03.01.json",
}

GOLDEN_MARK = re.compile(r"pytest\.mark\.golden\b")


def modules_carrying_the_mark(root: pathlib.Path) -> set[str]:
    found = set()
    for path in root.rglob("test_*.py"):
        relative = path.relative_to(root)
        if {".venv", "site-packages", ".pytest-tmp"} & set(relative.parts):
            continue
        if GOLDEN_MARK.search(path.read_text(encoding="utf-8")):
            found.add(relative.as_posix())
    return found


def test_the_command_is_a_marker_selection():
    assert GOLDEN_COMMAND == "pytest -m golden"


def test_pytest_ini_declares_the_marker_and_says_what_it_means():
    """``--strict-markers`` is on, so an undeclared mark would error rather than
    silently select nothing — but the DESCRIPTION is what a verifier reads."""
    config = configparser.ConfigParser()
    config.read(ROOT / "pytest.ini", encoding="utf-8")
    markers = config["pytest"]["markers"]
    assert "--strict-markers" in config["pytest"]["addopts"]
    assert re.search(r"^golden: THE GOLDEN SET \(D-283\)", markers, re.MULTILINE)
    assert re.search(r"^statute: .*NOT in the golden set", markers, re.MULTILINE)


@pytest.mark.parametrize("command", [VerifyCommand, ImportCommand], ids=["verify", "import"])
def test_both_commands_name_the_golden_command_in_their_help(command):
    parser = command().create_parser("manage.py", command.__module__.rsplit(".", 1)[-1])
    (action,) = [a for a in parser._actions if "--golden-tests-passed" in a.option_strings]
    assert GOLDEN_COMMAND in action.help
    assert "D-283" in action.help


def test_the_golden_set_is_exactly_the_listed_modules():
    assert modules_carrying_the_mark(ROOT) == set(GOLDEN_MODULES)


def test_no_statute_transcription_carries_the_golden_mark():
    for path in (ROOT / "calculators" / "tests").glob("test_*_statute.py"):
        assert not GOLDEN_MARK.search(path.read_text(encoding="utf-8")), path.name


def test_the_scan_fails_on_an_unlisted_golden_module(tmp_path):
    """PROVE EVERY GUARD FAILS: a new file adding the mark is found, so the
    set-equality above would refuse it until it is listed."""
    # Spelled in two halves so that THIS file does not carry the mark it scans for.
    mark = "pytest.mark." + "golden"
    (tmp_path / "test_new_thing.py").write_text(
        f"import pytest\npytestmark = {mark}\n", encoding="utf-8"
    )
    (tmp_path / "test_other.py").write_text("import pytest\n", encoding="utf-8")

    assert modules_carrying_the_mark(tmp_path) == {"test_new_thing.py"}
