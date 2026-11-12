"""Tests for the Flipper-compatible Markdown allowlist."""

import pytest

from tools.check_markdown import validate_markdown


def test_allowed_formatting_and_links_pass() -> None:
    """
    Accept shallow headings, emphasis, lists, and HTTP(S) links.

    The accepted syntax matches the subset used in catalog text fields.
    """
    assert (
        validate_markdown(
            "# Clock\n\n**Bold** and _italic_.\n\n- First\n- [Details](https://example.com)"
        )
        == []
    )


@pytest.mark.parametrize(
    ("value", "element", "line"),
    [
        ("first\n### Too deep", "Header depth", 2),
        ("first\n![image](https://example.com/a.png)", "image", 2),
        ("first\n<p>HTML</p>", "html", 2),
        ("first\n`inline code`", "backtick", 2),
        ("first\n\n    code block", "Code block", 3),
        ("first\n> quote", "Quote", 2),
        ("first\n\n---", "Horizontal line", 3),
        ("first\n[unsafe](javascript:alert(1))", "link URL", 2),
    ],
)
def test_forbidden_markdown_reports_source_line(
    value: str, element: str, line: int
) -> None:
    """
    Point authors to the one-based line containing a forbidden construct.

    Each disallowed feature is reported with its element name.
    """
    errors = validate_markdown(value)

    assert len(errors) == 1
    assert f"line {line}:" in errors[0]
    assert element in errors[0]


def test_reference_links_are_not_allowed() -> None:
    """
    Require explicit inline links instead of reference syntax.

    This keeps rendered descriptions self-contained for review.
    """
    errors = validate_markdown("[details][ref]\n\n[ref]: https://example.com")

    assert errors
    assert "Reference link" in errors[0]
