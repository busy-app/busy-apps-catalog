"""Tests for preparing approved build metadata for backend publication."""

import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from tools.lib_publication_data import prepare_publication


def test_prepare_approved_bundle(tmp_path: Path, make_png) -> None:
    """
    Derive hashed artifacts and notice data from a matching approved build output.

    An altered source origin must prevent publication.
    """
    build_dir = tmp_path / "build_dir"
    (build_dir / "media").mkdir(parents=True)
    original = {
        "sourcecode": {
            "type": "git",
            "location": {
                "origin": "https://github.com/owner/clock",
                "commit_sha": "a" * 40,
            },
        },
        "category": "clocks",
        "short_description": "A small clock for the bar",
        "description": "A small clock for the BUSY Bar that shows the time and the date.",
        "changelog": "Initial release",
        "screenshots": ["screen.png"],
    }
    appmeta = {
        "format_version": 1,
        "id": "app.busy.clock",
        "name": "Clock",
        "version": "1.0.0",
        "api_version": "27.14.0",
    }
    resolved = {
        **original,
        "screenshots": ["screenshot-1.png"],
        "appmeta": appmeta,
        "toolchain": "0.2.1",
    }
    (build_dir / "manifest.resolved.json").write_text(json.dumps(resolved))
    (build_dir / "sources.tar.gz").write_bytes(b"sources")

    (build_dir / "media" / "screenshot-1.png").write_bytes(make_png(72, 16))
    (build_dir / "media" / "icon.png").write_bytes(make_png(8, 8))
    with tarfile.open(
        build_dir / "package.tgz", "w:gz", format=tarfile.USTAR_FORMAT
    ) as tar:
        for name, content in {
            "app.busy.clock/appmeta/manifest.json": json.dumps(appmeta).encode(),
            "app.busy.clock/scripts/main.js": b"// test",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    manifest = tmp_path / "manifest.yml"
    manifest.write_text(yaml.safe_dump(original))
    results = tmp_path / "results.json"
    results.write_text('{"status":"pass","checks":[]}')
    data = prepare_publication(
        build_dir,
        results,
        manifest,
        tmp_path / "objects",
        7,
        "b" * 40,
        "c" * 40,
        "reviewer",
        "app.busy.clock",
    )
    assert data["alias"] == "app.busy.clock"
    assert data["package"]["key"].startswith("packages/")
    assert data["icon"]["key"].startswith("assets/")
    assert "ci_run_url" not in data
    with_run = prepare_publication(
        build_dir,
        results,
        manifest,
        tmp_path / "run",
        7,
        "b" * 40,
        "c" * 40,
        "reviewer",
        "app.busy.clock",
        "https://github.com/busy-app/x/actions/runs/9",
    )
    assert with_run["ci_run_url"].endswith("/actions/runs/9")
    assert "back_screenshots" not in data
    original["back_screenshots"] = ["back.png"]
    resolved["back_screenshots"] = ["back-screenshot-1.png"]
    manifest.write_text(yaml.safe_dump(original))
    (build_dir / "manifest.resolved.json").write_text(json.dumps(resolved))
    (build_dir / "media" / "back-screenshot-1.png").write_bytes(make_png(160, 80))
    with_back = prepare_publication(
        build_dir,
        results,
        manifest,
        tmp_path / "back",
        7,
        "b" * 40,
        "c" * 40,
        "reviewer",
        "app.busy.clock",
    )
    assert [item["key"][:7] for item in with_back["back_screenshots"]] == ["assets/"]
    (build_dir / "media" / "back-screenshot-1.png").write_bytes(make_png(72, 16))
    with pytest.raises(ValueError, match="160"):
        prepare_publication(
            build_dir,
            results,
            manifest,
            tmp_path / "bad",
            7,
            "b" * 40,
            "c" * 40,
            "reviewer",
            "app.busy.clock",
        )
    del original["back_screenshots"], resolved["back_screenshots"]
    manifest.write_text(yaml.safe_dump(original))
    (build_dir / "manifest.resolved.json").write_text(json.dumps(resolved))
    (build_dir / "media" / "icon.png").unlink()
    no_icon = prepare_publication(
        build_dir,
        results,
        manifest,
        tmp_path / "noicon",
        7,
        "b" * 40,
        "c" * 40,
        "reviewer",
        "app.busy.clock",
    )
    assert "icon" not in no_icon
    (build_dir / "media" / "icon.png").symlink_to(
        build_dir / "media" / "screenshot-1.png"
    )
    with pytest.raises(ValueError, match="icon path"):
        prepare_publication(
            build_dir,
            results,
            manifest,
            tmp_path / "link",
            7,
            "b" * 40,
            "c" * 40,
            "reviewer",
            "app.busy.clock",
        )
    (build_dir / "media" / "icon.png").unlink()
    (build_dir / "media" / "icon.png").write_bytes(make_png(8, 8))
    resolved["sourcecode"]["location"]["origin"] = "https://github.com/attacker/clock"
    (build_dir / "manifest.resolved.json").write_text(json.dumps(resolved))
    with pytest.raises(ValueError, match="sourcecode"):
        prepare_publication(
            build_dir,
            results,
            manifest,
            tmp_path / "other",
            7,
            "b" * 40,
            "c" * 40,
            "reviewer",
            "app.busy.clock",
        )
