"""Create a verified tar.gz, copy it, and restore it."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shelf.checksum import hash_file, hash_reader
from shelf.discover import Unit
from shelf.errors import ShelfError
from shelf.paths import ensure_inside_roots, is_denied_name, is_regenerable_name, is_venv_dir

PAYLOAD_PREFIX = "payload/"


@dataclass(frozen=True)
class ArchiveRecord:
    relative: str
    kind: str
    sha256: str
    size: int
    target: str


@dataclass(frozen=True)
class BuiltArchive:
    tar_path: Path
    manifest_path: Path
    sha256: str
    work_dir: Path
    archived_at: str


def create_archive(unit: Unit, *, when: datetime | None = None, progress: bool = True) -> BuiltArchive:
    moment = when or datetime.now(timezone.utc)
    archived_at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    base = f"{slug_for(unit.path)}-{stamp}"
    work_dir = Path(tempfile.mkdtemp(prefix="shelf-"))
    try:
        tar_path = work_dir / f"{base}.tar.gz"
        manifest_path = work_dir / f"{base}.manifest.json"
        records = _collect(unit.path, progress)
        manifest = {
            "version": 1,
            "original_path": str(unit.path),
            "root_kind": "dir" if unit.path.is_dir() else "file",
            "archived_at": archived_at,
            "file_count": sum(1 for record in records if record.kind == "file"),
            "upload_bytes": sum(record.size for record in records if record.kind == "file"),
            "files": [
                {
                    "path": record.relative,
                    "sha256": record.sha256,
                    "bytes": record.size,
                    "kind": record.kind,
                    "target": record.target,
                }
                for record in records
            ],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        _write_tar(tar_path, manifest_path, unit.path, records)
        verify_archive(tar_path, manifest)
        return BuiltArchive(
            tar_path=tar_path,
            manifest_path=manifest_path,
            sha256=hash_file(tar_path),
            work_dir=work_dir,
            archived_at=archived_at,
        )
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def slug_for(path: Path) -> str:
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        relative = path
    pieces: list[str] = []
    for part in relative.parts:
        cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in part)
        pieces.append(cleaned or "_")
    return "__".join(pieces)


def _excluded_dir(directory: Path, name: str) -> bool:
    path = directory / name
    if is_regenerable_name(name) or path.is_symlink():
        return True
    return is_venv_dir(path)


def _file_record(path: Path, relative: str) -> ArchiveRecord:
    return ArchiveRecord(relative, "file", hash_file(path), path.stat().st_size, "")


def _collect(root: Path, progress: bool) -> list[ArchiveRecord]:
    if root.is_symlink():
        raise ShelfError(f"refusing to archive symlink {root}")
    if root.is_file():
        return [_file_record(root, root.name)]
    if not root.is_dir():
        raise ShelfError(f"refusing to archive {root}")
    records: list[ArchiveRecord] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in dirnames:
            if is_denied_name(name):
                raise ShelfError(f"{root} contains protected folder {name}")
        dirnames[:] = sorted(name for name in dirnames if not _excluded_dir(base, name))
        for name in sorted(filenames):
            if name == ".DS_Store":
                continue
            full = base / name
            relative = full.relative_to(root).as_posix()
            if full.is_symlink():
                records.append(ArchiveRecord(relative, "symlink", "", 0, os.readlink(full)))
                continue
            if not full.is_file():
                continue
            records.append(_file_record(full, relative))
            if progress and len(records) % 500 == 0:
                print(f"shelf: hashing {len(records)} files in {root.name}", file=sys.stderr)
    return records


def _write_tar(
    tar_path: Path,
    manifest_path: Path,
    root: Path,
    records: list[ArchiveRecord],
) -> None:
    with tarfile.open(tar_path, "w:gz", compresslevel=1) as tar:
        tar.add(manifest_path, arcname="shelf-manifest.json", recursive=False)
        for record in records:
            source = root if root.is_file() else root / record.relative
            tar.add(source, arcname=f"{PAYLOAD_PREFIX}{record.relative}", recursive=False)
    _fsync(tar_path)


def _fsync(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        return


def verify_archive(tar_path: Path, manifest: dict[str, object]) -> None:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ShelfError("archive manifest has no file list")
    expected: dict[str, dict[str, object]] = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ShelfError("archive manifest has a bad file entry")
        expected[str(item["path"])] = item
    seen: set[str] = set()
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == "shelf-manifest.json":
                continue
            relative = payload_relative(member.name)
            record = expected.get(relative)
            if record is None:
                raise ShelfError(f"archive has unexpected member {member.name}")
            seen.add(relative)
            _verify_member(tar, member, relative, record)
    missing = set(expected) - seen
    if missing:
        raise ShelfError(f"archive is missing {sorted(missing)[0]}")


def _verify_member(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    relative: str,
    record: dict[str, object],
) -> None:
    kind = record.get("kind")
    if kind == "symlink":
        if not member.issym() or member.linkname != record.get("target"):
            raise ShelfError(f"symlink mismatch for {relative}")
        return
    if kind != "file":
        raise ShelfError(f"unsupported manifest entry for {relative}")
    if not member.isreg():
        raise ShelfError(f"{relative} is not a regular file in the archive")
    extracted = tar.extractfile(member)
    if extracted is None:
        raise ShelfError(f"missing content for {relative}")
    if hash_reader(extracted) != record.get("sha256"):
        raise ShelfError(f"hash mismatch for {relative}")
    if member.size != record.get("bytes"):
        raise ShelfError(f"size mismatch for {relative}")


def payload_relative(name: str) -> str:
    if not name.startswith(PAYLOAD_PREFIX):
        raise ShelfError(f"unexpected member {name}")
    relative = name[len(PAYLOAD_PREFIX) :]
    parts = Path(relative).parts
    if not relative or relative.startswith("/") or ".." in parts:
        raise ShelfError(f"unsafe member {name}")
    return relative


def copy_verified(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(destination) + ".partial")
    try:
        shutil.copyfile(source, partial)
        _fsync(partial)
        source_hash = hash_file(source)
        if hash_file(partial) != source_hash:
            raise ShelfError(f"copy verification failed for {destination.name}")
        partial.replace(destination)
        if hash_file(destination) != source_hash:
            destination.unlink(missing_ok=True)
            raise ShelfError(f"destination verification failed for {destination.name}")
        return source_hash
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def publish(built: BuiltArchive, destination: Path) -> tuple[Path, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    tar_dest = destination / built.tar_path.name
    manifest_dest = destination / built.manifest_path.name
    if tar_dest.exists() or manifest_dest.exists():
        raise ShelfError(f"archive already exists at {tar_dest}")
    copy_verified(built.tar_path, tar_dest)
    try:
        copy_verified(built.manifest_path, manifest_dest)
    except Exception:
        tar_dest.unlink(missing_ok=True)
        raise
    return tar_dest, manifest_dest


def write_pointer(
    unit: Unit,
    tar_dest: Path,
    manifest_dest: Path,
    digest: str,
    archived_at: str,
) -> Path:
    pointer = unit.path.parent / f"{unit.path.name}.shelf.json"
    payload = {
        "version": 1,
        "original_path": str(unit.path),
        "archive": str(tar_dest),
        "manifest": str(manifest_dest),
        "sha256": digest,
        "archived_at": archived_at,
    }
    pointer.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return pointer


def discard_work(built: BuiltArchive) -> None:
    shutil.rmtree(built.work_dir, ignore_errors=True)


def remove_local(path: Path, roots: tuple[Path, ...], destination: Path) -> None:
    if path.is_symlink():
        raise ShelfError(f"refusing to delete symlink {path}")
    resolved = ensure_inside_roots(path, roots)
    dest_resolved = destination.resolve()
    if resolved == dest_resolved or resolved.is_relative_to(dest_resolved):
        raise ShelfError(f"refusing to delete {path}; it is inside the archive destination")
    if dest_resolved.is_relative_to(resolved):
        raise ShelfError(f"refusing to delete {path}; the archive destination is inside it")
    if not resolved.exists():
        return
    if resolved.is_dir():
        shutil.rmtree(resolved)
        return
    if resolved.is_file():
        resolved.unlink()
        return
    raise ShelfError(f"refusing to delete {path}")


def resolve_archive(spec: str, destination: Path) -> Path:
    candidate = Path(spec).expanduser()
    if candidate.is_file() and candidate.name.endswith(".shelf.json"):
        data = json.loads(candidate.read_text(encoding="utf-8"))
        archive = Path(str(data["archive"]))
        if not archive.is_file():
            raise ShelfError(f"archive missing: {archive}")
        return archive
    if candidate.is_file() and candidate.name.endswith(".tar.gz"):
        return candidate
    if destination.is_dir():
        matches = sorted(destination.glob(f"*{Path(spec).name}*.tar.gz"))
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ShelfError("more than one archive matches; pass the .tar.gz path")
    raise ShelfError(f"no archive found for {spec}")


def restore_archive(archive: Path) -> Path:
    if not archive.is_file():
        raise ShelfError(f"archive not found: {archive}")
    with tarfile.open(archive, "r:gz") as tar:
        try:
            member = tar.getmember("shelf-manifest.json")
        except KeyError as exc:
            raise ShelfError("archive has no manifest") from exc
        extracted = tar.extractfile(member)
        if extracted is None:
            raise ShelfError("archive has no manifest")
        manifest = json.loads(extracted.read().decode("utf-8"))
    if manifest.get("version") != 1:
        raise ShelfError("unsupported archive version")
    verify_archive(archive, manifest)
    target = Path(str(manifest["original_path"]))
    if target.exists() or target.is_symlink():
        raise ShelfError(f"{target} already exists")
    if not target.parent.is_dir():
        raise ShelfError(f"parent folder does not exist: {target.parent}")
    staging = target.with_name(target.name + ".shelf-restore")
    if staging.exists() or staging.is_symlink():
        raise ShelfError(f"remove leftover {staging} first")
    try:
        _extract_payload(archive, staging)
        if manifest.get("root_kind") == "file":
            files = [item for item in staging.iterdir() if item.name != ".DS_Store"]
            if len(files) != 1 or not files[0].is_file():
                raise ShelfError("file archive did not contain one file")
            files[0].rename(target)
            staging.rmdir()
        else:
            staging.rename(target)
    except Exception:
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging, ignore_errors=True)
        elif staging.exists() or staging.is_symlink():
            staging.unlink(missing_ok=True)
        raise
    return target


def _extract_payload(archive: Path, target: Path) -> None:
    target.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name == "shelf-manifest.json":
                continue
            relative = payload_relative(member.name)
            destination = target / relative
            if not destination.resolve().is_relative_to(target.resolve()):
                raise ShelfError(f"unsafe path {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if member.issym():
                if destination.exists() or destination.is_symlink():
                    raise ShelfError(f"refusing to overwrite {destination}")
                destination.symlink_to(member.linkname)
                continue
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isreg():
                raise ShelfError(f"refusing to extract special file {member.name}")
            source = tar.extractfile(member)
            if source is None:
                raise ShelfError(f"missing content for {relative}")
            with destination.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            os.chmod(destination, member.mode & 0o777)
