"""Static screening of PHREEQC input.

PHREEQC's input language can read and write arbitrary paths (DATABASE, DUMP, INCLUDE$,
every `-file` option). Anything a user can type must therefore be screened before it
reaches the engine. This is layer one; the engine child process also runs with
RLIMIT_FSIZE = 0 so that a keyword we failed to anticipate still cannot write a file.
"""

from __future__ import annotations

import re

from ...domain.errors import UnsafeInputError, ValidationError

#: Keywords that touch the filesystem or swap the thermodynamic database.
FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "DATABASE",
    "DUMP",
    "INCLUDE$",
    "RUN_CELLS",  # only meaningful with file-backed transport in our deployment
)

#: Option forms that redirect output to a path.
_FILE_OPTION = re.compile(r"^\s*-\s*(\w*_?file|file)\b", re.IGNORECASE | re.MULTILINE)
_KEYWORD_LINE = re.compile(r"^\s*([A-Z][A-Z_0-9$]*)", re.MULTILINE)
_PHASE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_()\.\-+:]{0,63}$")


def sanitize_input(text: str, *, max_bytes: int) -> str:
    """Return the input unchanged, or raise. Never silently edits a scientist's model."""
    if not text or not text.strip():
        raise ValidationError("PHREEQC input is empty")

    encoded = text.encode("utf-8", errors="strict")
    if len(encoded) > max_bytes:
        raise ValidationError(
            f"PHREEQC input is {len(encoded)} bytes, limit is {max_bytes}",
            limit=max_bytes,
        )
    if "\x00" in text:
        raise UnsafeInputError("PHREEQC input contains a null byte")

    upper = text.upper()
    for keyword in FORBIDDEN_KEYWORDS:
        # A word boundary is wrong here: 'INCLUDE$' ends in a non-word character.
        if re.search(rf"^\s*{re.escape(keyword)}(?![A-Z0-9_])", upper, re.MULTILINE):
            raise UnsafeInputError(
                f"{keyword} is not permitted: it reads or writes files on the server",
                keyword=keyword,
            )

    match = _FILE_OPTION.search(text)
    if match:
        raise UnsafeInputError(
            f"file redirection option {match.group(0).strip()!r} is not permitted",
            option=match.group(0).strip(),
        )

    if "END" not in upper:
        raise ValidationError("PHREEQC input must be terminated with END")

    return text


def validate_phase_name(name: str) -> str:
    """Phase names are interpolated into generated input, so they are whitelisted."""
    if not _PHASE_NAME.match(name):
        raise ValidationError(f"invalid phase name {name!r}", phase=name)
    return name


def keywords_used(text: str) -> set[str]:
    """Coarse inventory of the keyword blocks present; used for metrics and routing."""
    return {m.group(1) for m in _KEYWORD_LINE.finditer(text.upper())}
