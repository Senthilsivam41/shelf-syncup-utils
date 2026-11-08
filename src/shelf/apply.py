"""Archive cold folders and delete regenerable dependency folders."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shelf.archive import create_archive, discard_work, publish, remove_local, write_pointer
from shelf.discover import Plan, RegenDir, Unit
from shelf.errors import ShelfError
from shelf.paths import allocated_bytes, format_bytes, is_regenerable_name


@dataclass(frozen=True)
class ApplyReport:
    lines: tuple[str, ...]
    failures: tuple[str, ...]


def apply_plan(plan: Plan, *, yes: bool) -> ApplyReport:
    if not yes:
        raise ShelfError("refusing to change files without --yes. Run: shelf plan")
    if not plan.cold and not plan.regenerable:
        return ApplyReport(("Nothing to archive or delete.",), ())
    if not plan.destination_ready:
        raise ShelfError(f"destination is not available: {plan.destination}")
    _require_space(plan)
    lines: list[str] = []
    for unit in plan.cold:
        lines.extend(_apply_unit(plan, unit))
    failures: list[str] = []
    for item in plan.regenerable:
        try:
            lines.extend(_apply_regen(plan, item))
        except ShelfError as exc:
            failures.append(str(exc))
            print(f"shelf: {exc}", file=sys.stderr)
    return ApplyReport(tuple(lines), tuple(failures))


def _apply_unit(plan: Plan, unit: Unit) -> list[str]:
    if unit.action == "pending":
        remove_local(unit.path, plan.roots, plan.destination)
        _record({"action": "remove-local", "path": str(unit.path)})
        return [f"removed local copy {unit.path}"]
    if unit.action != "cold":
        raise ShelfError(f"refusing to archive {unit.path} ({unit.action})")
    built = None
    try:
        built = create_archive(unit)
        tar_dest, manifest_dest = publish(built, plan.destination)
        write_pointer(unit, tar_dest, manifest_dest, built.sha256, built.archived_at)
        remove_local(unit.path, plan.roots, plan.destination)
        local = allocated_bytes(tar_dest.stat())
        _record(
            {
                "action": "archive",
                "path": str(unit.path),
                "archive": str(tar_dest),
                "sha256": built.sha256,
                "free_bytes": unit.free_bytes,
            }
        )
        return [
            f"archived {unit.path}",
            f"  {tar_dest}",
            f"  Google Drive copy currently occupies {format_bytes(local)} on this disk",
        ]
    finally:
        if built is not None:
            discard_work(built)


def _apply_regen(plan: Plan, item: RegenDir) -> list[str]:
    if not item.path.exists():
        return []
    if not is_regenerable_name(item.path.name):
        raise ShelfError(f"refusing to delete {item.path}; it is not a dependency folder")
    remove_local(item.path, plan.roots, plan.destination)
    _record({"action": "delete-deps", "path": str(item.path), "free_bytes": item.free_bytes})
    return [f"removed {item.path}"]


def _require_space(plan: Plan) -> None:
    if plan.upload_bytes <= 0:
        return
    usage_free = shutil.disk_usage(Path.home()).free
    needed = plan.upload_bytes * 2 + 512 * 1024 * 1024
    if usage_free < needed:
        raise ShelfError(
            f"need about {format_bytes(needed)} free during upload; "
            f"{format_bytes(usage_free)} free now"
        )


def history_path() -> Path:
    override = os.environ.get("SHELF_HISTORY")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "shelf" / "history.jsonl"


def _record(event: dict[str, object]) -> None:
    path = history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **event,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"shelf: history not written: {exc}", file=sys.stderr)
