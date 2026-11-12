"""Table-driven manifest contract regressions for external authors."""

import json
from pathlib import Path

import pytest
import yaml

from tools.check_manifest import validate

EXAMPLE = Path(__file__).parents[1] / "example/com.example.weather/manifest.yml"


def manifest(tmp_path, data):
    """
    Store a case at a valid app-ID path so only its targeted rule changes.

    YAML preserves useful field line locations for CLI annotations.
    """
    path = tmp_path / "applications/app.busy.clock/manifest.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_example_is_valid():
    """
    Validate the shipped pinned fixture example without a network dependency.

    Documentation examples must remain executable author guidance.
    """
    assert validate(EXAMPLE) == []


@pytest.mark.parametrize(
    "field",
    [
        "sourcecode",
        "category",
        "short_description",
        "description",
        "changelog",
        "screenshots",
    ],
)
def test_each_required_field(tmp_path, field):
    """
    Reject omission of each required catalog manifest field independently.

    A table case cannot accidentally rely on another missing field.
    """
    data = json.loads(EXAMPLE.read_text())
    data.pop(field)
    assert validate(manifest(tmp_path, data))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit_sha", "a" * 39),
        ("commit_sha", "a" * 41),
        ("commit_sha", "z" * 40),
        ("origin", "http://github.com/a/b"),
        ("origin", "https://example.test/a/b"),
        ("subdir", "../outside"),
        ("subdir", "/absolute"),
    ],
)
def test_source_fields(tmp_path, field, value):
    """
    Reject unsupported source references and directory escape syntax.

    The complete SHA is mandatory even for an otherwise valid manifest.
    """
    data = json.loads(EXAMPLE.read_text())
    data["sourcecode"]["location"][field] = value
    assert validate(manifest(tmp_path, data))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("screenshots", []),
        ("screenshots", ["a.png"] * 11),
        ("short_description", "a" * 121),
        ("short_description", "a" * 9),
        ("short_description", "first\nsecond"),
        ("description", "a" * 4001),
        ("description", "@../outside"),
        ("description", "@/absolute"),
        ("changelog", ""),
        ("category", "unknown"),
        ("short_description", "@README.md"),
        ("category", "@category"),
        ("extra", "unexpected"),
    ],
)
def test_manifest_fields(tmp_path, field, value):
    """
    Reject invalid field limits, references and unknown properties separately.

    The fixture remains otherwise equal to the accepted example.
    """
    data = json.loads(EXAMPLE.read_text())
    data[field] = value
    assert validate(manifest(tmp_path, data))


@pytest.mark.parametrize("count", [1, 10])
@pytest.mark.parametrize("suffix", ["", ".git"])
def test_screenshot_boundaries_and_origin_suffix(tmp_path, count, suffix):
    """
    Accept both screenshot count boundaries and the documented optional git suffix.

    Schema validation does not fetch image bytes or repository contents.
    """
    data = json.loads(EXAMPLE.read_text())
    data["screenshots"] = ["screenshots/clock.png"] * count
    data["sourcecode"]["location"]["origin"] += suffix
    assert validate(manifest(tmp_path, data)) == []
