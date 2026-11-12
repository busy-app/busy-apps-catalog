"""Shared test helpers."""

import struct
import zlib

import pytest

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def chunk(kind: bytes, payload: bytes) -> bytes:
    """Build a PNG chunk with its checksum."""
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


@pytest.fixture
def make_png():
    """Return a builder of minimal PNG files with optional extra chunks and header fields."""

    def build(
        width: int,
        height: int,
        *,
        depth: int = 8,
        color: int = 0,
        extra: tuple[tuple[bytes, bytes], ...] = (),
        late: tuple[tuple[bytes, bytes], ...] = (),
    ) -> bytes:
        """
        Create a non-interlaced PNG filled with zero samples.

        `extra` chunks go before IDAT, `late` ones after it.
        """
        header = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0)
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
        row = b"\0" + b"\0" * ((width * channels * depth + 7) // 8)
        return (
            SIGNATURE
            + chunk(b"IHDR", header)
            + b"".join(chunk(kind, payload) for kind, payload in extra)
            + chunk(b"IDAT", zlib.compress(row * height))
            + b"".join(chunk(kind, payload) for kind, payload in late)
            + chunk(b"IEND", b"")
        )

    build.chunk = chunk
    return build


@pytest.fixture
def mock_http(monkeypatch):
    """
    Route `tools.lib_http` through a handler instead of the network.

    Returns a setter: `mock_http(handler)`, where the handler takes an httpx2.Request
    and returns an httpx2.Response. Pauses between retries are recorded, not slept.
    """
    import httpx2

    from tools import lib_http

    sleeps: list[float] = []
    monkeypatch.setattr(lib_http.time, "sleep", sleeps.append)
    monkeypatch.setattr(lib_http.random, "uniform", lambda low, high: 0)

    def install(handler):
        monkeypatch.setattr(lib_http, "TRANSPORT", httpx2.MockTransport(handler))
        lib_http.reset()
        return sleeps

    yield install
    lib_http.reset()
