"""Tests for license recognition by the commit's tree, without network."""

from pathlib import Path

import pytest

from tools.check_registry import CheckError
from tools.lib_license import check_license, detect

TEXTS = Path(__file__).parent / "licenses"


@pytest.mark.parametrize("spdx", ["MIT", "Apache-2.0", "GPL-3.0", "BSD-3-Clause"])
def test_real_texts_are_recognised(spdx: str) -> None:
    """Standard texts give their SPDX id."""
    assert detect((TEXTS / f"{spdx}.txt").read_text()) == spdx


def test_other_texts_are_not_recognised() -> None:
    """Proprietary, altered and commercial-use-restricted texts are not recognised."""
    mit = (TEXTS / "MIT.txt").read_text()
    assert detect("All rights reserved. Do not copy.") is None
    assert detect(mit.replace("without restriction", "for personal use only")) is None
    assert (
        detect(
            mit
            + "\nThis software may not be used for commercial purposes (non-commercial)."
        )
        is None
    )
    assert detect("") is None


def test_files_are_searched_in_the_application_directory_and_root(
    tmp_path: Path,
) -> None:
    """`LICENSE.md` in the subdir and `COPYING` in the root both count; none is an error."""
    root = tmp_path
    source = root / "app"
    source.mkdir()
    with pytest.raises(CheckError) as error:
        check_license(root, source)
    assert error.value.id == "license"
    (source / "LICENSE.md").write_text((TEXTS / "MIT.txt").read_text())
    assert check_license(root, source) == "MIT"
    (source / "LICENSE.md").unlink()
    (root / "COPYING").write_text((TEXTS / "GPL-3.0.txt").read_text())
    assert check_license(root, source) == "GPL-3.0"
    (root / "COPYING").write_text("proprietary")
    assert check_license(root, source) is None


def test_symlinked_license_is_ignored(tmp_path: Path) -> None:
    """A symlink named LICENSE is not read."""
    (tmp_path / "real.txt").write_text((TEXTS / "MIT.txt").read_text())
    (tmp_path / "LICENSE").symlink_to(tmp_path / "real.txt")
    with pytest.raises(CheckError):
        check_license(tmp_path, tmp_path)
