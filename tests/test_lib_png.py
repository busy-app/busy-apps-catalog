"""Tests for PNG normalization: one clean RGBA format, same pixels, same bytes."""

import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from tools.check_png import SCREENSHOT, validate_png
from tools.lib_png import normalize_png


def chunks(path: Path) -> list[bytes]:
    """Names of the chunks of a PNG file, in order."""
    data = path.read_bytes()
    offset = 8
    names = []
    while offset < len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        names.append(data[offset + 4 : offset + 8])
        offset += 12 + size
    return names


def make(path: Path, mode: str, **save) -> None:
    """Save a small image with a colorful pattern in the given mode."""
    base = Image.new("RGBA", (8, 8))
    base.putdata(
        [(x * 30 % 256, y * 20 % 256, 90, 255) for y in range(8) for x in range(8)]
    )
    base.convert(mode).save(path, format="PNG", **save)


def test_metadata_palette_and_interlace_become_clean_rgba(tmp_path: Path) -> None:
    """tEXt/eXIf/iCCP, palette, grayscale and interlaced inputs end as IHDR/IDAT/IEND."""
    info = PngInfo()
    info.add_text("Comment", "hello")
    cases = {
        "text": ("RGBA", {"pnginfo": info}),
        "icc": ("RGB", {"icc_profile": b"x" * 20}),
        "palette": ("P", {}),
        "gray": ("L", {}),
        "interlaced": ("RGBA", {"interlace": 1}),
    }
    for name, (mode, save) in cases.items():
        path = tmp_path / f"{name}.png"
        make(path, mode, **save)
        before = Image.open(path).convert("RGBA")
        normalize_png(path, (8, 8))
        assert chunks(path) == [b"IHDR", b"IDAT", b"IEND"], name
        after = Image.open(path)
        assert after.mode == "RGBA" and after.tobytes() == before.tobytes()
        header = path.read_bytes()[24:29]
        assert header[0] == 8 and header[1] == 6 and header[4] == 0, name


def test_exif_chunk_is_removed(tmp_path: Path) -> None:
    """An eXIf chunk disappears."""
    path = tmp_path / "exif.png"
    image = Image.new("RGBA", (8, 8), (1, 2, 3, 255))
    exif = Image.Exif()
    exif[270] = "x"
    image.save(path, format="PNG", exif=exif)
    assert b"eXIf" in chunks(path)
    normalize_png(path, (8, 8))
    assert chunks(path) == [b"IHDR", b"IDAT", b"IEND"]


def test_result_is_deterministic_and_stable(tmp_path: Path) -> None:
    """The same input gives the same bytes; normalizing again changes nothing."""
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    make(first, "RGBA")
    second.write_bytes(first.read_bytes())
    normalize_png(first, (8, 8))
    normalize_png(second, (8, 8))
    assert first.read_bytes() == second.read_bytes()
    once = first.read_bytes()
    normalize_png(first, (8, 8))
    assert first.read_bytes() == once


def test_normalized_screenshot_still_passes_validation(tmp_path: Path) -> None:
    """The strict validator accepts the normalized file (checked again before publish)."""
    path = tmp_path / "s.png"
    Image.new("L", (72, 16), 7).save(path, format="PNG")
    normalize_png(path, (72, 16))
    validate_png(path, *SCREENSHOT)


def test_validation_does_not_need_pillow(tmp_path: Path, monkeypatch) -> None:
    """`validate_png` rejects bad files without ever calling Pillow."""

    def boom(*args, **kwargs):
        raise AssertionError("Pillow must not be used for validation")

    monkeypatch.setattr(Image, "open", boom)
    from tools.check_registry import CheckError

    bad = tmp_path / "bad.png"
    for content in (b"nope", b"\x89PNG\r\n\x1a\n", zlib.compress(b"x")):
        bad.write_bytes(content)
        with pytest.raises(CheckError):
            validate_png(bad, (8, 8), "icon_png", 64 * 1024)


def test_transparency_is_kept(tmp_path: Path) -> None:
    """tRNS on a palette and on 8-bit gray survive as alpha."""
    path = tmp_path / "p.png"
    image = Image.new("P", (8, 8), 0)
    image.putpalette([10, 20, 30, 200, 100, 50])
    image.save(path, format="PNG", transparency=0)
    normalize_png(path, (8, 8))
    assert Image.open(path).convert("RGBA").getpixel((0, 0)) == (10, 20, 30, 0)
    gray = tmp_path / "g.png"
    Image.new("L", (8, 8), 77).save(gray, format="PNG", transparency=77)
    normalize_png(gray, (8, 8))
    assert Image.open(gray).convert("RGBA").getpixel((0, 0)) == (77, 77, 77, 0)


def test_only_png_is_opened(tmp_path: Path) -> None:
    """A GIF with the right size is not decoded by Pillow."""
    path = tmp_path / "x.png"
    Image.new("P", (8, 8)).save(path, format="GIF")
    with pytest.raises(ValueError):
        normalize_png(path, (8, 8))


def test_wrong_size_is_rejected_without_decoding(tmp_path: Path) -> None:
    """The size is compared before pixels are loaded; no global Pillow limit is touched."""
    path = tmp_path / "x.png"
    Image.new("RGBA", (16, 16)).save(path, format="PNG")
    before = Image.MAX_IMAGE_PIXELS
    with pytest.raises(ValueError, match="16×16"):
        normalize_png(path, (8, 8))
    assert Image.MAX_IMAGE_PIXELS == before
