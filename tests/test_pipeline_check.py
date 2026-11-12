"""Checks that an incomplete pipeline cannot report success."""

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import patch

from tools.pipeline_check import stage_results


def test_missing_build_output_blocks_publication() -> None:
    """
    Report build failure and missing mandatory checks as failures.

    The report is structured even when no build output is available.
    """
    result = stage_results(None, "app.busy.clock")
    assert result["status"] == "fail"
    assert {item["id"] for item in result["checks"]} == {
        "build",
        "author_attestation",
    }
    assert result["skipped"] == ["package", "catalog"]


def test_author_attestation_still_needs_human_review() -> None:
    """A checkbox is an author declaration, not automated proof of permission."""
    result = stage_results(None, "app.busy.clock", author_attested=True)
    assert "author_attestation" not in {item["id"] for item in result["checks"]}
    assert result["author_attested"] is True


def test_build_check_error_becomes_a_failing_check(tmp_path: Path) -> None:
    """A CheckError written by the build is reported under its own check id."""
    error = tmp_path / "build-error.json"
    error.write_text(json.dumps({"id": "source_commit", "detail": "a" * 40}))
    result = stage_results(
        None, "app.busy.clock", author_attested=True, build_error=error
    )
    assert result["status"] == "fail"
    assert result["checks"] == [
        {"id": "source_commit", "result": "fail", "detail": "a" * 40}
    ]
    error.write_text(json.dumps({"id": "no_such_check"}))
    result = stage_results(
        None, "app.busy.clock", author_attested=True, build_error=error
    )
    assert result["checks"][0]["id"] == "build"


def test_stage_results_includes_source_diff_for_same_repository(tmp_path: Path) -> None:
    """Carry the published-to-submitted GitHub compare URL into report results."""
    app_id = "app.busy.clock"
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    package = build_dir / "package.tgz"
    files = {
        f"{app_id}/appmeta/manifest.json": json.dumps(
            {
                "format_version": 1,
                "id": app_id,
                "name": "Clock",
                "version": "1.1.0",
                "api_version": "27.14.0",
            }
        ).encode(),
        f"{app_id}/scripts/main.js": b"print('clock')",
    }
    with tarfile.open(package, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    (build_dir / "manifest.resolved.json").write_text(
        json.dumps(
            {
                "sourcecode": {
                    "location": {
                        "origin": "https://github.com/acme/clock",
                        "commit_sha": "b" * 40,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    catalog = type(
        "FakeCatalog",
        (),
        {
            "application": lambda self, _: {
                "current_version": {
                    "version": "1.0.0",
                    "links": {
                        "source_code": {
                            "uri": "https://github.com/acme/clock",
                            "commit_sha": "a" * 40,
                        }
                    },
                }
            },
            "api_lines": lambda self: [
                {"is_latest_release": True, "is_release": True, "api": "27.14.0"}
            ],
        },
    )()

    with patch("tools.pipeline_check.HttpCatalogSource", return_value=catalog):
        result = stage_results(build_dir, app_id, "https://catalog.example", True)

    assert result["compare_url"] == (
        f"https://github.com/acme/clock/compare/{'a' * 40}...{'b' * 40}"
    )
    assert result["status"] == "pass"


def test_failed_build_diagnostics_reach_comment_result(tmp_path):
    """
    Preserve a bounded actionable build error while keeping the result failed.

    Missing api_version and compiler syntax errors must be visible without log navigation.
    """
    log = tmp_path / "build-error.txt"
    log.write_text(
        "noise\nValueError: Add api_version to manifest.json\nSyntax error in main.ts\n"
    )
    result = stage_results(None, "app.busy.clock", author_attested=True, build_log=log)
    assert result["status"] == "fail"
    assert result["checks"][0]["id"] == "build"
    assert "api_version" in result["checks"][0]["detail"]
    assert "Syntax error" in result["checks"][0]["detail"]
    assert len(result["checks"][0]["detail"]) < 2000


def test_copied_manifest_location_is_not_checked_from_its_path(tmp_path: Path) -> None:
    """A valid copy in `prepared/` gives no `manifest_path`; the id comes from prepare."""
    from tools.pipeline_check import stage_results

    copy = tmp_path / "prepared" / "manifest.yml"
    copy.parent.mkdir()
    copy.write_bytes(
        (
            Path(__file__).parents[1] / "example/com.example.weather/manifest.yml"
        ).read_bytes()
    )
    result = stage_results(None, "app.busy.clock", manifest=copy)
    assert "manifest_path" not in {item["id"] for item in result["checks"]}


def test_missing_front_icon_is_a_warning(tmp_path: Path) -> None:
    """A build output without `media/icon.png` gets the optional `icon_missing` finding, not a failure."""
    app_id = "app.busy.clock"
    build_dir = tmp_path / "build"
    (build_dir / "media").mkdir(parents=True)
    result = stage_results(build_dir, app_id, author_attested=True)
    missing = [item for item in result["checks"] if item["id"] == "icon_missing"]
    assert (
        missing and result["status"] == "fail"
    )  # the empty build output has no package at all
    (build_dir / "media" / "icon.png").write_bytes(b"x")
    result = stage_results(build_dir, app_id, author_attested=True)
    assert "icon_missing" not in {item["id"] for item in result["checks"]}


def test_license_result_and_warning(tmp_path: Path) -> None:
    """A recognised license is carried into the results; an unknown text is a warning."""
    bundle = tmp_path / "bundle"
    (bundle / "media").mkdir(parents=True)
    (bundle / "license.json").write_text('{"spdx": "MIT"}')
    result = stage_results(bundle, "app.busy.clock", author_attested=True)
    assert result["license"] == "MIT"
    assert "license_unrecognised" not in {item["id"] for item in result["checks"]}
    (bundle / "license.json").write_text('{"spdx": null}')
    result = stage_results(bundle, "app.busy.clock", author_attested=True)
    assert result["license"] is None
    assert "license_unrecognised" in {item["id"] for item in result["checks"]}
    (bundle / "license.json").write_text('{"spdx": "Evil <b>"}')
    assert (
        stage_results(bundle, "app.busy.clock", author_attested=True)["license"] is None
    )
