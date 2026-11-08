"""Command line for planning, applying, and restoring shelf archives."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from shelf import __version__
from shelf.apply import apply_plan
from shelf.archive import resolve_archive, restore_archive
from shelf.discover import Plan, RegenDir, Unit, build_plan
from shelf.errors import ShelfError
from shelf.paths import DEFAULT_IDLE_DAYS, default_destination, default_roots, format_bytes


def main(argv: list[str] | None = None) -> int:
    raw = _normalize(list(sys.argv[1:] if argv is None else argv))
    parser = _parser()
    args = parser.parse_args(raw)
    try:
        if args.command == "plan":
            return command_plan(args)
        if args.command == "apply":
            return command_apply(args)
        if args.command == "restore":
            return command_restore(args)
        raise ShelfError(f"unknown command {args.command}")
    except ShelfError as exc:
        print(f"shelf: {exc}", file=sys.stderr)
        return 1


def command_plan(args: argparse.Namespace) -> int:
    plan = make_plan(args, progress=not args.json)
    if args.json:
        print(json.dumps(plan_document(plan), indent=2))
        return 0
    print(render_plan(plan))
    print("Nothing was changed.")
    print("Apply with: shelf apply --yes")
    return 0


def command_apply(args: argparse.Namespace) -> int:
    plan = make_plan(args, progress=not args.json)
    if args.json:
        print(json.dumps(plan_document(plan), indent=2))
    else:
        print(render_plan(plan))
        if args.yes:
            print("Applying.")
    report = apply_plan(plan, yes=args.yes)
    for line in report.lines:
        print(line)
    if report.failures:
        print(f"shelf: {len(report.failures)} delete(s) failed", file=sys.stderr)
        return 1
    return 0


def command_restore(args: argparse.Namespace) -> int:
    destination = args.dest.expanduser() if args.dest else default_destination()
    archive = resolve_archive(args.spec, destination)
    restored = restore_archive(archive)
    print(f"Restored {restored}")
    return 0


def make_plan(args: argparse.Namespace, *, progress: bool) -> Plan:
    roots = tuple(path.expanduser() for path in args.root) if args.root else default_roots()
    destination = args.dest.expanduser() if args.dest else default_destination()
    return build_plan(
        roots=roots,
        destination=destination,
        days=args.days,
        progress=progress,
    )


def plan_document(plan: Plan) -> dict[str, object]:
    usage = shutil.disk_usage(Path.home())
    return {
        "days": plan.days,
        "generated_at": plan.generated_at,
        "destination": str(plan.destination),
        "destination_ready": plan.destination_ready,
        "roots": [str(root) for root in plan.roots],
        "upload_bytes": plan.upload_bytes,
        "free_bytes": plan.free_bytes,
        "disk_free_bytes": usage.free,
        "empty_skipped": plan.empty_skipped,
        "warnings": list(plan.warnings),
        "cold": [_unit_document(unit) for unit in plan.cold],
        "regenerable": [
            {"path": str(item.path), "free_bytes": item.free_bytes} for item in plan.regenerable
        ],
        "denied": [_unit_document(unit) for unit in plan.denied],
    }


def _unit_document(unit: Unit) -> dict[str, object]:
    return {
        "path": str(unit.path),
        "action": unit.action,
        "idle_days": unit.idle_days,
        "upload_bytes": unit.upload_bytes,
        "free_bytes": unit.free_bytes,
        "file_count": unit.file_count,
        "git_dirty": unit.git_dirty,
        "reason": unit.reason,
        "secrets": list(unit.secrets),
        "regenerable": [
            {"path": str(item.path), "free_bytes": item.free_bytes} for item in unit.regenerable
        ],
    }


def render_plan(plan: Plan) -> str:
    lines = [
        "Shelf plan",
        f"  idle:  {plan.days} days",
        f"  dest:  {plan.destination}",
        f"  roots: {', '.join(str(root) for root in plan.roots)}",
        f"  when:  {plan.generated_at}",
        "",
    ]
    fresh = [unit for unit in plan.cold if unit.action == "cold"]
    pending = [unit for unit in plan.cold if unit.action == "pending"]
    if fresh:
        upload = sum(unit.upload_bytes for unit in fresh)
        freeing = sum(unit.free_bytes for unit in fresh)
        lines.append(
            f"Archive, verify, then remove {len(fresh)} item(s) "
            f"({format_bytes(freeing)} local, {format_bytes(upload)} to upload)"
        )
        for unit in fresh:
            lines.append(f"  {unit.idle_days:>4}d  {format_bytes(unit.free_bytes):>10}  {unit.path}")
            if unit.git_dirty:
                lines.append("         git working tree has uncommitted changes; they will be included")
            _append_secrets(lines, unit.secrets)
            _append_left_out(lines, unit.path, unit.regenerable)
        lines.append("")
    else:
        lines.append("No idle folders to archive.")
        lines.append("")
    if pending:
        lines.append("Already archived; local copy will be removed")
        for unit in pending:
            lines.append(f"  {format_bytes(unit.free_bytes):>10}  {unit.path}")
        lines.append("")
    if plan.regenerable:
        dep_bytes = sum(item.free_bytes for item in plan.regenerable)
        lines.append(
            f"Delete dependency folders in folders you still use ({format_bytes(dep_bytes)})"
        )
        _append_deletes(lines, plan.regenerable)
        lines.append("")
    if plan.denied:
        lines.append("Leave in place")
        for unit in plan.denied:
            lines.append(f"  {unit.path}  ({unit.reason})")
        lines.append("")
    if plan.empty_skipped:
        lines.append(f"Skipped {plan.empty_skipped} empty folder(s).")
        lines.append("")
    for warning in plan.warnings:
        lines.append(f"warning: {warning}")
    if plan.warnings:
        lines.append("")
    usage = shutil.disk_usage(Path.home())
    lines.append(f"Free space now: {format_bytes(usage.free)}")
    lines.append(f"Estimated space freed after apply: {format_bytes(plan.free_bytes)}")
    if fresh:
        lines.append(
            "Extra space used during upload, before originals are deleted: "
            f"about {format_bytes(plan.upload_bytes * 2)}"
        )
    lines.append(
        "Archives go to Google Drive. Drive may keep its own local cache until "
        "the Shelf folder is set to online-only."
    )
    return "\n".join(lines)


def _append_left_out(lines: list[str], root: Path, items: tuple[RegenDir, ...]) -> None:
    large, small = _split_by_size(items)
    for item in large:
        try:
            label = str(item.path.relative_to(root))
        except ValueError:
            label = item.path.name
        lines.append(
            f"         left out of the archive: {label} ({format_bytes(item.free_bytes)})"
        )
    if small:
        total = sum(item.free_bytes for item in small)
        noun = "folder" if len(small) == 1 else "folders"
        lines.append(
            "         left out of the archive: "
            f"{len(small)} smaller dependency {noun} ({format_bytes(total)})"
        )


def _append_deletes(lines: list[str], items: tuple[RegenDir, ...]) -> None:
    large, small = _split_by_size(items)
    for item in large:
        lines.append(f"  {format_bytes(item.free_bytes):>10}  {item.path}")
    if small:
        total = sum(item.free_bytes for item in small)
        noun = "folder" if len(small) == 1 else "folders"
        lines.append(f"  {format_bytes(total):>10}  {len(small)} smaller dependency {noun}")


def _split_by_size(items: tuple[RegenDir, ...]) -> tuple[list[RegenDir], list[RegenDir]]:
    large = [item for item in items if item.free_bytes >= 1_000_000]
    small = [item for item in items if item.free_bytes < 1_000_000]
    return large, small


def _append_secrets(lines: list[str], secrets: tuple[str, ...]) -> None:
    shown = secrets[:10]
    for secret in shown:
        lines.append(f"         secret file will be uploaded: {secret}")
    extra = len(secrets) - len(shown)
    if extra > 0:
        lines.append(f"         and {extra} more secret files")


def _normalize(argv: list[str]) -> list[str]:
    if not argv:
        return ["plan"]
    head = argv[0]
    if head in {"plan", "apply", "restore", "-h", "--help", "--version"}:
        return argv
    if head.startswith("-"):
        return ["plan", *argv]
    return argv


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shelf",
        description=(
            "Back up folders that have sat idle, verify the Google Drive copy, "
            "then remove the local originals. Dependency folders are deleted "
            "without being archived."
        ),
    )
    parser.add_argument("--version", action="version", version=f"shelf {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--days", type=int, default=DEFAULT_IDLE_DAYS)
    common.add_argument("--root", action="append", type=Path)
    common.add_argument("--dest", type=Path)
    common.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("plan", parents=[common], help="Show what would be archived and deleted")
    apply_parser = sub.add_parser(
        "apply",
        parents=[common],
        help="Archive, verify, then delete",
    )
    apply_parser.add_argument("--yes", action="store_true", help="Actually change files")
    restore_parser = sub.add_parser("restore", parents=[common], help="Restore one archive")
    restore_parser.add_argument("spec", help="Archive path, pointer path, or project name")
    return parser
