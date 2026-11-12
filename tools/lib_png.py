"""Rewrite checked PNG files into one clean format (RGBA, 8 bit, no ancillary chunks)."""

from pathlib import Path

from PIL import Image


def normalize_png(path: Path, dimensions: tuple[int, int]) -> None:
    """
    Rewrite the file in place as RGBA PNG without metadata chunks.

    Run only after `validate_png`, which already rejects the formats that cannot be
    converted without loss (16 bits, transparency keys on low-bit gray). Anything
    unexpected raises ValueError; the pixels must stay the same and the same input
    always gives the same bytes.
    """
    try:
        with Image.open(path, formats=["PNG"]) as source:
            if source.size != dimensions:
                raise ValueError(f"is {source.width}×{source.height}")
            if source.mode == "P":
                palette = source.palette.getdata()[1] if source.palette else b""
                if source.tobytes() and max(source.tobytes()) >= len(palette) // 3:
                    raise ValueError("uses a color outside its palette")
            expected = source.convert("RGBA")
        expected.info = {}  # drop ICC profile, EXIF and text carried over from the source
        expected.save(path, format="PNG", optimize=False, compress_level=9)
        with Image.open(path, formats=["PNG"]) as saved:
            if saved.mode != "RGBA" or saved.tobytes() != expected.tobytes():
                raise ValueError("pixels changed")
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"{type(error).__name__}: {error}") from error
