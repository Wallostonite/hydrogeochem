from __future__ import annotations

import pytest

from hgc.domain.errors import UnsafeInputError, ValidationError
from hgc.services.phreeqc.sanitizer import sanitize_input, validate_phase_name

SAFE = "SOLUTION 1\n    pH 7 charge\n    Na 1\n    Cl 1\nEND\n"


def test_safe_input_passes_through_unchanged():
    assert sanitize_input(SAFE, max_bytes=1000) == SAFE


@pytest.mark.parametrize(
    "hostile",
    [
        "DATABASE /etc/passwd\nSOLUTION 1\nEND\n",
        "DUMP\n    -file /tmp/leak.txt\nEND\n",
        "SELECTED_OUTPUT\n    -file /tmp/out.tsv\nEND\n",
        "INCLUDE$ /etc/shadow\nEND\n",
    ],
)
def test_filesystem_keywords_are_refused(hostile):
    with pytest.raises(UnsafeInputError):
        sanitize_input(hostile, max_bytes=10_000)


def test_oversized_input_is_refused():
    with pytest.raises(ValidationError):
        sanitize_input("SOLUTION 1\nEND\n" + "# padding\n" * 10_000, max_bytes=1_000)


def test_input_without_end_is_refused():
    with pytest.raises(ValidationError):
        sanitize_input("SOLUTION 1\n    pH 7\n", max_bytes=1000)


def test_phase_names_are_whitelisted():
    assert validate_phase_name("CO2(g)") == "CO2(g)"
    with pytest.raises(ValidationError):
        validate_phase_name("Calcite\n    -file /tmp/x")
