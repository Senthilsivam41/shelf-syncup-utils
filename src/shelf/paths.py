"""Paths shelf is willing to archive, skip, or delete."""

from __future__ import annotations

import os
import re
from pathlib import Path

from shelf.errors import ShelfError

DEFAULT_IDLE_DAYS = 90
MIN_FILE_BYTES = 1_000_000

# Whole directories that must stay on this Mac and must not be uploaded.
DENY_NAMES = frozenset({"passworddb", "recovery codes", ".ssh"})

# Dependency and build folders. They are deleted locally and left out of archives.
REGENERABLE_NAMES = frozenset(
    {
        "node_modules",
        ".next",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".turbo",
        ".code-review-graph",
        ".parcel-cache",
        ".vite",
        "htmlcov",
        ".pnpm-store",
    }
)
_VENV_RE = re.compile(r"^\.?venv\d*$")

_SECRET_NAMES = frozenset(
    {
        ".env",
        "credentials.json",
        "secrets.json",
        "id_rsa",
        "id_ed25519",
        ".netrc",
    }
)
_SECRET_SUFFIXES = (".pem", ".p12", ".kdbx", ".key")


def is_denied_name(name: str) -> bool:
    return name.casefold() in DENY_NAMES


def is_regenerable_name(name: str) -> bool:
    if name in REGENERABLE_NAMES:
        return True
    return _VENV_RE.fullmatch(name) is not None


def is_venv_dir(path: Path) -> bool:
    """True for a virtualenv whose folder name is not literally `venv`."""
    return (path / "pyvenv.cfg").is_file()


def is_secret_name(name: str) -> bool:
    if name == "cacert.pem" or name.endswith((".example", ".sample", ".template")):
        return False
    if name in _SECRET_NAMES or name.startswith(".env."):
        return True
    return name.endswith(_SECRET_SUFFIXES)


def default_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (home / "work", home / "personal")


def default_destination() -> Path:
    cloud = Path.home() / "Library" / "CloudStorage"
    drives = sorted(path for path in cloud.glob("GoogleDrive-*") if path.is_dir())
    for drive in drives:
        my_drive = drive / "My Drive"
        if my_drive.is_dir():
            return my_drive / "Shelf"
    raise ShelfError(
        "Google Drive is not available. Expected a My Drive folder under "
        f"{cloud}/GoogleDrive-*."
    )


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def allocated_bytes(stat_result: os.stat_result) -> int:
    blocks = getattr(stat_result, "st_blocks", None)
    if isinstance(blocks, int):
        return blocks * 512
    return stat_result.st_size


def ensure_inside_roots(path: Path, roots: tuple[Path, ...]) -> Path:
    """Return the resolved path when it is strictly inside one scan root."""
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise ShelfError(f"refusing to delete {path}")
    for root in roots:
        root_resolved = root.resolve()
        if resolved == root_resolved:
            raise ShelfError(f"refusing to delete scan root {root}")
        if resolved.is_relative_to(root_resolved):
            return resolved
    raise ShelfError(f"{path} is outside the scan roots")
