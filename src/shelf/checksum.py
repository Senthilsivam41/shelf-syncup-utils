"""Streaming checksums for archives and the copies uploaded to Drive."""

import hashlib
from pathlib import Path
from typing import BinaryIO


def hash_reader(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def hash_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hash_reader(handle)
