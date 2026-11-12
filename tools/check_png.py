"""Validate PNG files (screenshots, icons) without executing untrusted code."""

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from tools.check_registry import CheckError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_DECOMPRESSED = 1024 * 1024
SCREENSHOT = ((72, 16), "screenshot_png", 2 * 1024 * 1024)
SCREENSHOT_FINAL = ((72, 16), "screenshot_final", 2 * 1024 * 1024)
BACK_SCREENSHOT = ((160, 80), "back_screenshot_png", 2 * 1024 * 1024)
BACK_SCREENSHOT_FINAL = ((160, 80), "back_screenshot_final", 2 * 1024 * 1024)
ICON_FRONT = ((8, 8), "icon_png", 64 * 1024)
ICON_BACK = ((11, 11), "icon_png", 64 * 1024)


VALID_DEPTHS = {
    0: {1, 2, 4, 8, 16},
    2: {8, 16},
    3: {1, 2, 4, 8},
    4: {8, 16},
    6: {8, 16},
}
CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
# Adam7 passes as (x start, y start, x step, y step); a plain image is one pass.
PLAIN_PASS = ((0, 0, 1, 1),)
ADAM7_PASSES = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


@dataclass(frozen=True)
class Header:
    """The IHDR fields the later checks need."""

    bit_depth: int
    color: int
    interlace: int


def invalid(check_id: str, reason: str) -> CheckError:
    """A structural PNG error, worded the same way for every check."""
    return CheckError(check_id, f"is not a valid PNG: {reason}")


def read_header(payload: bytes, dimensions: tuple[int, int], check_id: str) -> Header:
    """Check the IHDR payload against the expected size and the supported formats."""
    if len(payload) != 13:
        raise invalid(check_id, "PNG header missing")
    width, height = struct.unpack_from(">II", payload)
    if (width, height) != dimensions:
        raise CheckError(
            check_id, f"is {width}×{height}, expected {dimensions[0]}×{dimensions[1]}"
        )
    bit_depth, color, compression, filtering, interlace = payload[8:]
    if (
        color not in VALID_DEPTHS
        or bit_depth not in VALID_DEPTHS[color]
        or compression != 0
        or filtering != 0
        or interlace not in (0, 1)
    ):
        raise invalid(check_id, "Unsupported PNG header")
    if bit_depth == 16:
        raise CheckError(check_id, "uses 16 bits per channel; save it as an 8-bit PNG")
    return Header(bit_depth, color, interlace)


def read_chunks(
    data: bytes, dimensions: tuple[int, int], check_id: str
) -> tuple[Header, list[bytes], set[bytes]]:
    """
    Walk the chunks after the signature, checking framing and checksums.

    Returns the header, the IDAT payloads and the set of chunk types seen.
    """
    offset = len(PNG_SIGNATURE)
    header = None
    idat: list[bytes] = []
    seen: set[bytes] = set()
    ended = False
    while offset + 12 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        stop = offset + 12 + size
        if stop > len(data):
            raise invalid(check_id, "Truncated PNG chunk")
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + size]
        expected = struct.unpack_from(">I", data, offset + 8 + size)[0]
        if zlib.crc32(kind + payload) != expected:
            raise invalid(check_id, "Invalid PNG checksum")
        if header is None:
            if kind != b"IHDR":
                raise invalid(check_id, "PNG header missing")
            header = read_header(payload, dimensions, check_id)
        elif kind == b"IHDR":
            raise invalid(check_id, "Duplicate PNG header")
        seen.add(kind)
        if kind == b"IDAT":
            idat.append(payload)
        if kind == b"IEND":
            if size != 0 or stop != len(data):
                raise invalid(check_id, "Invalid PNG ending")
            ended = True
            break
        offset = stop
    if header is None or not ended or not idat:
        raise invalid(check_id, "Incomplete PNG image")
    return header, idat, seen


def inflate(idat: list[bytes], check_id: str) -> bytes:
    """Decompress the image data within the size limit; trailing data is an error."""
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(b"".join(idat), MAX_DECOMPRESSED + 1)
    except zlib.error as exc:
        raise invalid(check_id, "Invalid compressed PNG image data") from exc
    if len(pixels) > MAX_DECOMPRESSED or not decoder.eof or decoder.unused_data:
        raise invalid(check_id, "Invalid or oversized PNG image data")
    return pixels


def check_scanlines(
    pixels: bytes, header: Header, dimensions: tuple[int, int], check_id: str
) -> None:
    """Every row starts with a known filter byte and the rows fill the data exactly."""
    width, height = dimensions
    channels = CHANNELS[header.color]
    passes = ADAM7_PASSES if header.interlace else PLAIN_PASS
    position = 0
    for x, y, dx, dy in passes:
        pass_width = max(0, (width - x + dx - 1) // dx)
        pass_height = max(0, (height - y + dy - 1) // dy)
        if not pass_width or not pass_height:
            continue
        row_size = (pass_width * channels * header.bit_depth + 7) // 8
        for _ in range(pass_height):
            if position >= len(pixels) or pixels[position] > 4:
                raise invalid(check_id, "Invalid PNG scanline")
            position += row_size + 1
    if position != len(pixels):
        raise invalid(check_id, "Invalid PNG pixel data length")


def size_limit_text(max_bytes: int) -> str:
    """The limit as the author reads it: whole MiB, otherwise KiB."""
    if max_bytes % (1 << 20) == 0:
        return f"{max_bytes >> 20} MiB"
    return f"{max_bytes >> 10} KiB"


def validate_png(
    path: Path, dimensions: tuple[int, int], check_id: str, max_bytes: int
) -> None:
    """
    Validate PNG chunk framing, checksums, dimensions and compressed pixels.

    The file must be a complete PNG of exactly `dimensions` and stay within `max_bytes`;
    errors carry `check_id`. No image decoder is involved.
    """
    if path.stat().st_size > max_bytes:
        raise CheckError(check_id, f"is larger than {size_limit_text(max_bytes)}")
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise CheckError(check_id, "is not a PNG file")
    header, idat, seen = read_chunks(data, dimensions, check_id)
    if header.color == 3 and b"PLTE" not in seen:
        raise invalid(check_id, "Palette is missing")
    if header.color == 0 and header.bit_depth < 8 and b"tRNS" in seen:
        raise CheckError(
            check_id,
            "has a transparency key on a low-bit grayscale image; save it as 8-bit RGBA",
        )
    check_scanlines(inflate(idat, check_id), header, dimensions, check_id)
