"""Find cold folders and regenerable dependency directories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Never

from shelf.checksum import hash_file
from shelf.errors import ShelfError
from shelf.paths import (
    DEFAULT_IDLE_DAYS,
    MIN_FILE_BYTES,
    allocated_bytes,
    default_destination,
    default_roots,
    is_denied_name,
    is_regenerable_name,
    is_secret_name,
    is_venv_dir,
)

Action = Literal["cold", "warm", "denied", "empty", "pending"]


class _Denied(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _assert_never(value: Never) -> Never:
    raise ShelfError(f"unexpected action {value!r}")


@dataclass(frozen=True)
class RegenDir:
    path: Path
    free_bytes: int


@dataclass(frozen=True)
class Unit:
    path: Path
    action: Action
    idle_days: int
    upload_bytes: int
    free_bytes: int
    file_count: int
    regenerable: tuple[RegenDir, ...]
    secrets: tuple[str, ...]
    git_dirty: bool | None
    reason: str


@dataclass(frozen=True)
class Plan:
    roots: tuple[Path, ...]
    destination: Path
    destination_ready: bool
    days: int
    generated_at: str
    cold: tuple[Unit, ...]
    regenerable: tuple[RegenDir, ...]
    denied: tuple[Unit, ...]
    warnings: tuple[str, ...]
    empty_skipped: int

    @property
    def upload_bytes(self) -> int:
        return sum(unit.upload_bytes for unit in self.cold)

    @property
    def free_bytes(self) -> int:
        cold_free = sum(unit.free_bytes for unit in self.cold)
        dep_free = sum(item.free_bytes for item in self.regenerable)
        return cold_free + dep_free


class _Walker:
    def __init__(self) -> None:
        self.upload_bytes = 0
        self.archive_alloc = 0
        self.file_count = 0
        self.newest = 0.0
        self.secrets: list[str] = []
        self.regenerable: list[RegenDir] = []
        self.warnings: list[str] = []

    def walk(self, directory: Path, relative: str, regen_total: list[int] | None) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            self.warnings.append(f"{directory}: {exc.strerror or exc}")
            return
        for entry in entries:
            self._entry(entry, relative, regen_total)

    def _entry(
        self,
        entry: os.DirEntry[str],
        relative: str,
        regen_total: list[int] | None,
    ) -> None:
        name = entry.name
        if is_denied_name(name):
            raise _Denied(name)
        child_rel = f"{relative}/{name}" if relative else name
        try:
            if entry.is_symlink():
                if regen_total is None and is_secret_name(name):
                    self.secrets.append(child_rel)
                return
            if entry.is_dir(follow_symlinks=False):
                self._directory(Path(entry.path), name, child_rel, regen_total)
                return
            if entry.is_file(follow_symlinks=False):
                self._file(entry, name, child_rel, regen_total)
        except OSError as exc:
            self.warnings.append(f"{entry.path}: {exc.strerror or exc}")

    def _directory(
        self,
        path: Path,
        name: str,
        child_rel: str,
        regen_total: list[int] | None,
    ) -> None:
        if regen_total is None and (is_regenerable_name(name) or is_venv_dir(path)):
            bucket = [0]
            self.walk(path, child_rel, bucket)
            self.regenerable.append(RegenDir(path=path, free_bytes=bucket[0]))
            return
        self.walk(path, child_rel, regen_total)

    def _file(
        self,
        entry: os.DirEntry[str],
        name: str,
        child_rel: str,
        regen_total: list[int] | None,
    ) -> None:
        if name == ".DS_Store":
            return
        stat_result = entry.stat(follow_symlinks=False)
        allocated = allocated_bytes(stat_result)
        if regen_total is not None:
            regen_total[0] += allocated
            return
        self.upload_bytes += stat_result.st_size
        self.archive_alloc += allocated
        self.file_count += 1
        if stat_result.st_mtime > self.newest:
            self.newest = stat_result.st_mtime
        if is_secret_name(name):
            self.secrets.append(child_rel)


def looks_like_project(path: Path) -> bool:
    if (path / ".git").is_dir():
        return True
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        if (child / ".git").is_dir():
            return True
    return False


def is_container(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir() or (path / ".git").exists():
        return False
    found = 0
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        if looks_like_project(child):
            found += 1
            if found >= 2:
                return True
    return False


def iter_candidates(root: Path, warnings: list[str]) -> list[Path]:
    if not root.exists():
        warnings.append(f"missing root {root}")
        return []
    if not root.is_dir():
        warnings.append(f"not a directory {root}")
        return []
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        warnings.append(f"{root}: {exc.strerror or exc}")
        return []
    found: list[Path] = []
    for child in children:
        if child.name.startswith("."):
            continue
        if child.is_symlink():
            warnings.append(f"skipped symlink {child}")
            continue
        if child.is_dir() and not is_denied_name(child.name) and is_container(child):
            found.extend(_container_children(child, warnings))
            continue
        found.append(child)
    return found


def _container_children(container: Path, warnings: list[str]) -> list[Path]:
    try:
        children = sorted(container.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        warnings.append(f"{container}: {exc.strerror or exc}")
        return []
    found: list[Path] = []
    for child in children:
        if child.name.startswith("."):
            continue
        if child.is_symlink():
            warnings.append(f"skipped symlink {child}")
            continue
        found.append(child)
    return found


def _idle_days(newest: float, now: float) -> int:
    if newest <= 0:
        return 0
    return int((now - newest) // 86400)


def _unit(
    path: Path,
    *,
    action: Action,
    idle_days: int = 0,
    upload_bytes: int = 0,
    free_bytes: int = 0,
    file_count: int = 0,
    regenerable: tuple[RegenDir, ...] = (),
    secrets: tuple[str, ...] = (),
    reason: str = "",
) -> Unit:
    return Unit(
        path=path,
        action=action,
        idle_days=idle_days,
        upload_bytes=upload_bytes,
        free_bytes=free_bytes,
        file_count=file_count,
        regenerable=regenerable,
        secrets=secrets,
        git_dirty=None,
        reason=reason,
    )


def classify(path: Path, now: float, days: int, warnings: list[str]) -> Unit | None:
    if path.name.endswith(".shelf.json"):
        return None
    if is_denied_name(path.name):
        return _unit(path, action="denied", reason=f"protected name {path.name}")
    if path.is_symlink():
        warnings.append(f"skipped symlink {path}")
        return None
    if path.is_file():
        return _classify_file(path, now, days, warnings)
    if not path.is_dir():
        warnings.append(f"skipped {path}")
        return None
    if is_regenerable_name(path.name):
        total = [0]
        walker = _Walker()
        try:
            walker.walk(path, path.name, total)
        except _Denied as denied:
            warnings.extend(walker.warnings)
            return _unit(path, action="denied", reason=f"contains protected folder {denied.name}")
        warnings.extend(walker.warnings)
        return _unit(
            path,
            action="warm",
            regenerable=(RegenDir(path=path, free_bytes=total[0]),),
            free_bytes=total[0],
        )
    walker = _Walker()
    try:
        walker.walk(path, "", None)
    except _Denied as denied:
        warnings.extend(walker.warnings)
        return _unit(path, action="denied", reason=f"contains protected folder {denied.name}")
    warnings.extend(walker.warnings)
    newest = walker.newest
    if newest <= 0:
        try:
            newest = path.stat().st_mtime
        except OSError:
            newest = now
    age = _idle_days(newest, now)
    regenerable = tuple(walker.regenerable)
    free_bytes = walker.archive_alloc + sum(item.free_bytes for item in regenerable)
    if walker.file_count == 0 and free_bytes == 0:
        return _unit(path, action="empty", idle_days=age)
    if walker.file_count > 0 and age >= days:
        return _unit(
            path,
            action="cold",
            idle_days=age,
            upload_bytes=walker.upload_bytes,
            free_bytes=free_bytes,
            file_count=walker.file_count,
            regenerable=regenerable,
            secrets=tuple(walker.secrets),
        )
    return _unit(
        path,
        action="warm",
        idle_days=age,
        upload_bytes=walker.upload_bytes,
        free_bytes=free_bytes,
        file_count=walker.file_count,
        regenerable=regenerable,
        secrets=tuple(walker.secrets),
    )


def _classify_file(path: Path, now: float, days: int, warnings: list[str]) -> Unit | None:
    if is_secret_name(path.name):
        return _unit(path, action="denied", reason=f"protected file {path.name}")
    try:
        stat_result = path.stat()
    except OSError as exc:
        warnings.append(f"{path}: {exc.strerror or exc}")
        return None
    age = _idle_days(stat_result.st_mtime, now)
    if stat_result.st_size < MIN_FILE_BYTES or age < days:
        return None
    allocated = allocated_bytes(stat_result)
    return _unit(
        path,
        action="cold",
        idle_days=age,
        upload_bytes=stat_result.st_size,
        free_bytes=allocated,
        file_count=1,
        secrets=(path.name,) if is_secret_name(path.name) else (),
    )


def pointer_path(path: Path) -> Path:
    return path.parent / f"{path.name}.shelf.json"


def pointer_state(path: Path) -> Literal["absent", "verified", "mismatch", "missing"]:
    pointer = pointer_path(path)
    if not pointer.is_file():
        return "absent"
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        archive = Path(data["archive"])
        expected = str(data["sha256"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return "mismatch"
    if not archive.is_file():
        return "missing"
    if hash_file(archive) != expected:
        return "mismatch"
    return "verified"


def _apply_pointer(unit: Unit, warnings: list[str]) -> Unit:
    state = pointer_state(unit.path)
    if state == "absent":
        return unit
    if unit.action == "cold" and state == "verified":
        return replace(unit, action="pending", upload_bytes=0)
    if state == "verified" and unit.action == "warm":
        warnings.append(
            f"{unit.path} was archived earlier and has been used since; left in place"
        )
        return unit
    if state in {"mismatch", "missing"} and unit.action == "cold":
        warnings.append(
            f"archive pointer for {unit.path} is {state}; a new archive will be created"
        )
    return unit


def _git_repos(path: Path) -> list[Path]:
    if (path / ".git").exists():
        return [path]
    repos: list[Path] = []
    try:
        children = list(path.iterdir())
    except OSError:
        return []
    for child in children:
        if child.is_dir() and not child.is_symlink() and (child / ".git").exists():
            repos.append(child)
    return repos


def git_is_dirty(path: Path) -> bool | None:
    repos = _git_repos(path)
    if not repos:
        return None
    dirty = False
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    for repo in repos:
        try:
            completed = subprocess.run(
                [
                    "git",
                    f"--git-dir={repo / '.git'}",
                    f"--work-tree={repo}",
                    "status",
                    "--porcelain",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        if completed.stdout.strip():
            dirty = True
    return dirty


def _destination_ready(destination: Path) -> bool:
    if destination.is_dir():
        return True
    parent = destination.parent
    return parent.is_dir() and not destination.exists()


def build_plan(
    roots: tuple[Path, ...] | None = None,
    destination: Path | None = None,
    days: int = DEFAULT_IDLE_DAYS,
    now: float | None = None,
    progress: bool = False,
) -> Plan:
    if days < 1:
        raise ShelfError("--days must be at least 1")
    chosen = tuple(roots) if roots is not None else default_roots()
    dest = destination if destination is not None else default_destination()
    moment = time.time() if now is None else now
    warnings: list[str] = []
    cold: list[Unit] = []
    regenerable: list[RegenDir] = []
    denied: list[Unit] = []
    empty = 0
    for root in chosen:
        for candidate in iter_candidates(root, warnings):
            if progress:
                print(f"shelf: scanning {candidate}", file=sys.stderr)
            unit = classify(candidate, moment, days, warnings)
            if unit is None:
                continue
            unit = _apply_pointer(unit, warnings)
            match unit.action:
                case "cold" | "pending":
                    if unit.action == "cold":
                        unit = replace(unit, git_dirty=git_is_dirty(unit.path))
                    cold.append(unit)
                case "warm":
                    regenerable.extend(unit.regenerable)
                case "denied":
                    denied.append(unit)
                case "empty":
                    empty += 1
                case _ as unreachable:
                    _assert_never(unreachable)
    cold.sort(key=lambda item: item.free_bytes, reverse=True)
    regenerable.sort(key=lambda item: item.free_bytes, reverse=True)
    generated = datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Plan(
        roots=chosen,
        destination=dest,
        destination_ready=_destination_ready(dest),
        days=days,
        generated_at=generated,
        cold=tuple(cold),
        regenerable=tuple(regenerable),
        denied=tuple(denied),
        warnings=tuple(warnings),
        empty_skipped=empty,
    )
