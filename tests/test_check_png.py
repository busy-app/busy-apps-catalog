"""Tests for deterministic screenshot validation."""

from pathlib import Path

import pytest

from tools.check_png import SCREENSHOT, validate_png


def test_screenshot_png_validation(tmp_path: Path, make_png) -> None:
    """
    Accept a correct frame and reject wrong dimensions and corrupt data.

    Validation uses synthetic files only and never contacts a bar.
    """
    path = tmp_path / "screen.png"
    path.write_bytes(make_png(72, 16))
    validate_png(path, *SCREENSHOT)
    path.write_bytes(make_png(72, 17))
    with pytest.raises(ValueError):
        validate_png(path, *SCREENSHOT)
    path.write_bytes(make_png(72, 16)[:-5])
    with pytest.raises(ValueError):
        validate_png(path, *SCREENSHOT)
    path.write_bytes(b"not a PNG")
    with pytest.raises(ValueError):
        validate_png(path, *SCREENSHOT)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"depth": 16, "color": 0}, "16 bits"),
        ({"depth": 16, "color": 2}, "16 bits"),
        (
            {"depth": 2, "color": 0, "extra": ((b"tRNS", b"\x00\x01"),)},
            "transparency key",
        ),
        ({"depth": 8, "color": 3}, "Palette is missing"),
    ],
)
def test_formats_that_cannot_be_converted_losslessly_are_rejected(
    tmp_path: Path, make_png, kwargs, message
) -> None:
    """16 bits, a low-bit gray transparency key and a palette without PLTE are refused."""
    path = tmp_path / "i.png"
    path.write_bytes(make_png(8, 8, **kwargs))
    with pytest.raises(ValueError, match=message):
        validate_png(path, (8, 8), "icon_png", 64 * 1024)


def test_palette_with_plte_and_8_bit_gray_with_trns_pass(
    tmp_path: Path, make_png
) -> None:
    """Plain 8-bit grayscale (even with tRNS) and a palette image with PLTE are fine."""
    path = tmp_path / "i.png"
    path.write_bytes(make_png(8, 8, color=3, extra=((b"PLTE", b"\0\0\0"),)))
    validate_png(path, (8, 8), "icon_png", 64 * 1024)
    path.write_bytes(make_png(8, 8, color=0, extra=((b"tRNS", b"\x00\x00"),)))
    validate_png(path, (8, 8), "icon_png", 64 * 1024)
