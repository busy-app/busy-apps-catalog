"""The application ID rule shared by every check and schema."""

import re

MAX_LENGTH = 63
CHARS = r"[a-z0-9_-]+"
APP_ID = re.compile(rf"^(?=.{{3,{MAX_LENGTH}}}$){CHARS}(?:\.{CHARS}){{2,}}$")
APP_ID_PATTERN = rf"^{CHARS}(\.{CHARS}){{2,}}$"
MANIFEST_PATH = re.compile(
    r"applications/(?=[^/]{3," + str(MAX_LENGTH) + r"}/)"
    rf"{CHARS}(?:\.{CHARS}){{2,}}/manifest\.yml"
)
DOMAIN_PREFIXES = {"app", "com", "io", "org", "net", "dev"}
RULE = (
    f"Application ID: up to {MAX_LENGTH} characters, lower-case Latin letters, digits, "
    "`_`, `-` and `.`; at least two dots, none at the start or end or in a row."
)
EXAMPLES = (
    "e.g. `com.example.weather`, or `io.github.<your-github-user>.<app>` "
    "if you don't have a website."
)


def is_valid(app_id: str) -> bool:
    """Check the full rule: dotted parts of `[a-z0-9_-]`, at least three, up to 63."""
    return APP_ID.fullmatch(app_id) is not None


def is_recommended(app_id: str) -> bool:
    """
    Tell whether a valid ID starts like a reverse domain.

    The first part is a known top-level label or a two-letter country code.
    """
    first = app_id.split(".", 1)[0]
    return first in DOMAIN_PREFIXES or (len(first) == 2 and first.isalpha())


def invalid_reason(app_id: str) -> str:
    """Name the first reason an application ID fails the rule."""
    lower = app_id.lower()
    if lower != app_id and is_valid(lower):
        return f"use lower case — `{lower}`"
    bad = sorted({char for char in lower if not re.fullmatch(r"[a-z0-9_.-]", char)})
    if bad:
        chars = ", ".join(f"`{char}`" for char in bad)
        return f"{chars} {'is' if len(bad) == 1 else 'are'} not allowed"
    if len(app_id) > MAX_LENGTH:
        return f"longer than {MAX_LENGTH} characters"
    if "" in app_id.split("."):
        return "empty part between dots"
    return "needs at least two dots"
