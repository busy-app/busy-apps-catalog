"""The one safe way to look into package archives: rules for entries, limits, JSON members."""

import json
import tarfile
from pathlib import PurePosixPath

MAX_ENTRIES = 10000
MAX_UNPACKED = 128 * 1024 * 1024
MAX_JSON_MEMBER = 1024 * 1024


def path_is_safe(name: str, root: str) -> bool:
    """True for a relative path inside `<root>/`: no `..`, no absolute path, no backslash."""
    parts = PurePosixPath(name).parts
    return (
        bool(parts)
        and parts[0] == root
        and ".." not in parts
        and not (name.startswith("/") or "\\" in name)
    )


def has_extended_header(member: tarfile.TarInfo) -> bool:
    """True if the entry carries pax data or is a pax header: only plain ustar is accepted."""
    return bool(member.pax_headers) or member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE)


def is_plain_entry(member: tarfile.TarInfo) -> bool:
    """True for a regular file or a directory; links, devices and fifos are refused."""
    return member.isfile() or member.isdir()


def load_json_member(archive: tarfile.TarFile, name: str) -> object:
    """
    Parse a JSON file from the archive without unpacking it.

    A missing, non-regular or oversized member is a `ValueError`.
    """
    try:
        member = archive.getmember(name)
    except KeyError:
        raise ValueError(f"Missing {name}") from None
    file = archive.extractfile(member) if member.isfile() else None
    if file is None or member.size > MAX_JSON_MEMBER:
        raise ValueError(f"Unreadable {name}")
    return json.load(file)
