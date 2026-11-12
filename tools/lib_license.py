"""Recognise the open source license of an application repository without any network."""

import re
from pathlib import Path

from tools.check_registry import CheckError

LICENSE_FILE = re.compile(r"(licen[cs]e|copying)(\..+)?", re.IGNORECASE)
MAX_LICENSE_BYTES = 200 * 1024
RED_FLAGS = (
    "commons clause",
    "non-commercial",
    "noncommercial",
    "not for commercial",
    "no commercial",
    "for non commercial",
)
# (SPDX id, phrases that must all be present in the normalized text); the first match wins,
# so the more specific licenses come before the ones whose text they contain.
RULES = (
    ("AGPL-3.0", ("gnu affero general public license", "version 3, 19 november 2007")),
    ("LGPL-3.0", ("gnu lesser general public license", "version 3, 29 june 2007")),
    ("LGPL-2.1", ("gnu lesser general public license", "version 2.1, february 1999")),
    ("GPL-3.0", ("gnu general public license", "version 3, 29 june 2007")),
    ("GPL-2.0", ("gnu general public license", "version 2, june 1991")),
    ("Apache-2.0", ("apache license", "version 2.0, january 2004")),
    ("MPL-2.0", ("mozilla public license version 2.0",)),
    (
        "Unlicense",
        ("this is free and unencumbered software released into the public domain",),
    ),
    (
        "MIT",
        (
            "permission is hereby granted, free of charge, to any person obtaining a copy",
            "to deal in the software without restriction",
            'the software is provided "as is"',
        ),
    ),
    (
        "BSD-3-Clause",
        (
            "redistribution and use in source and binary forms, with or without modification, are permitted provided",
            "redistributions in binary form must reproduce",
            "neither the name of",
        ),
    ),
    (
        "BSD-2-Clause",
        (
            "redistribution and use in source and binary forms, with or without modification, are permitted provided",
            "redistributions in binary form must reproduce",
            "this software is provided by the copyright holders and contributors",
        ),
    ),
    (
        "ISC",
        (
            "permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies",
        ),
    ),
    (
        "0BSD",
        (
            "permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted.",
        ),
    ),
    (
        "Zlib",
        (
            "the origin of this software must not be misrepresented",
            "altered source versions must be plainly marked as such",
        ),
    ),
)
ALLOWED = frozenset(spdx for spdx, _ in RULES)


def detect(text: str) -> str | None:
    """SPDX id of a recognised open source license text, or None."""
    normalized = " ".join(
        text.lower().replace("’", "'").replace("“", '"').replace("”", '"').split()
    )
    if any(flag in normalized for flag in RED_FLAGS):
        return None
    for spdx, phrases in RULES:
        if all(phrase in normalized for phrase in phrases):
            return spdx
    return None


def license_files(directory: Path) -> list[Path]:
    """Regular LICENSE, LICENCE and COPYING files directly inside a directory."""
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if LICENSE_FILE.fullmatch(path.name)
        and path.is_file()
        and not path.is_symlink()
    )


def check_license(root: Path, source: Path) -> str | None:
    """
    Find the license of the application at the checked-out commit.

    The application directory is searched first, then the repository root. No file is
    an error; files that are not recognised return None (a warning, the moderator decides).
    """
    candidates = license_files(source)
    if source != root:
        candidates += license_files(root)
    if not candidates:
        raise CheckError(
            "license",
            "no LICENSE, LICENCE or COPYING file in the repository at this commit",
        )
    for path in candidates:
        if path.stat().st_size > MAX_LICENSE_BYTES:
            continue
        found = detect(path.read_text(encoding="utf-8", errors="replace"))
        if found is not None:
            return found
    return None
