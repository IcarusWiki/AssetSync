"""Per-texture fingerprints read straight from a pak, without extracting it.

UE pak v10+ indexes omit each file's SHA1, but every file's data is preceded by an
inline entry header that still carries it, so one small seek per file is enough.
A texture is the set of files sharing a stem (.uasset/.uexp/.ubulk); its fingerprint
combines the hashes of all its parts.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

from pyuepak import PakFile
from pyuepak.entry import Entry
from pyuepak.file_io import Reader

from . import SCOPE_PREFIX


def file_hashes(pak_path: Path, prefix: str = SCOPE_PREFIX) -> dict[str, str]:
    """Map each file path under ``prefix`` to the SHA1 of its stored bytes."""
    pak = PakFile()
    pak.read(pak_path)
    reader = Reader(str(pak_path))
    hashes = {}
    try:
        for path, entry in pak._index.entrys.items():
            if not path.startswith(prefix):
                continue
            reader.set_pos(entry.offset)
            hashes[path] = Entry().read(reader, pak.version).hash.hex()
    finally:
        reader.close()
    return hashes


def texture_hashes(files: dict[str, str]) -> dict[str, str]:
    """Group file hashes by asset stem; only stems with a .uasset count as assets."""
    parts: dict[str, dict[str, str]] = defaultdict(dict)
    for path, sha1 in files.items():
        stem, _, ext = path.rpartition(".")
        parts[stem][ext] = sha1
    combined = {}
    for stem, by_ext in parts.items():
        if "uasset" not in by_ext:
            continue
        digest = hashlib.sha1()
        for ext in sorted(by_ext):
            digest.update(f"{ext}:{by_ext[ext]}\n".encode())
        combined[stem] = digest.hexdigest()
    return combined


def scan(pak_paths: list[Path], prefix: str = SCOPE_PREFIX) -> dict[str, str]:
    files: dict[str, str] = {}
    for pak_path in pak_paths:
        files.update(file_hashes(pak_path, prefix))
    return texture_hashes(files)
