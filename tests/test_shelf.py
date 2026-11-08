"""Shelf scans, archives, and deletes only after the copy checks out."""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from shelf.apply import apply_plan
from shelf.archive import (
    create_archive,
    discard_work,
    payload_relative,
    remove_local,
    restore_archive,
    verify_archive,
)
from shelf.checksum import hash_file
from shelf.cli import main
from shelf.discover import build_plan
from shelf.errors import ShelfError
from shelf.paths import MIN_FILE_BYTES


def write_aged(path: Path, days: int, now: float, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    stamp = now - days * 86400
    os.utime(path, (stamp, stamp))


class ShelfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shelf-test-"))
        self.history = self.tmp / "history.jsonl"
        self._old_history = os.environ.get("SHELF_HISTORY")
        os.environ["SHELF_HISTORY"] = str(self.history)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if self._old_history is None:
            os.environ.pop("SHELF_HISTORY", None)
        else:
            os.environ["SHELF_HISTORY"] = self._old_history
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ninety_days_is_cold_and_eighty_nine_stays(self) -> None:
        now = time.time()
        root = self.tmp / "work"
        write_aged(root / "old" / "notes.txt", 90, now, "old")
        write_aged(root / "recent" / "notes.txt", 89, now, "new")
        plan = build_plan((root,), self.tmp / "Shelf", days=90, now=now)
        cold = {unit.path.name: unit for unit in plan.cold}
        self.assertEqual(set(cold), {"old"})
        self.assertEqual(cold["old"].action, "cold")
        self.assertEqual(cold["old"].idle_days, 90)

    def test_container_splits_repo_children(self) -> None:
        now = time.time()
        root = self.tmp / "work"
        write_aged(root / "repo" / "old" / "a.txt", 200, now, "a")
        (root / "repo" / "old" / ".git").mkdir(parents=True)
        write_aged(root / "repo" / "new" / "b.txt", 1, now, "b")
        (root / "repo" / "new" / ".git").mkdir(parents=True)
        plan = build_plan((root,), self.tmp / "Shelf", days=90, now=now)
        cold_names = {unit.path.name for unit in plan.cold}
        self.assertIn("old", cold_names)
        self.assertNotIn("new", cold_names)
        self.assertNotIn("repo", cold_names)

    def test_recent_venv_does_not_keep_old_project_warm(self) -> None:
        now = time.time()
        root = self.tmp / "work"
        write_aged(root / "old" / "keep.txt", 200, now, "keep")
        write_aged(root / "old" / "venv311" / "pkg.txt", 1, now, "wheel")
        plan = build_plan((root,), self.tmp / "Shelf", days=90, now=now)
        self.assertEqual([unit.path.name for unit in plan.cold], ["old"])
        self.assertEqual(plan.cold[0].regenerable[0].path.name, "venv311")
        self.assertEqual(plan.regenerable, ())

    def test_named_virtualenv_is_left_out_of_the_archive(self) -> None:
        now = time.time()
        root = self.tmp / "work"
        write_aged(root / "old" / "keep.txt", 200, now, "keep")
        write_aged(root / "old" / "myenv" / "pyvenv.cfg", 1, now, "home = /tmp\n")
        write_aged(root / "old" / "myenv" / "lib" / "pkg.txt", 1, now, "wheel")
        write_aged(root / "old" / ".env.example", 200, now, "TOKEN=\n")
        write_aged(root / "old" / "certs" / "cacert.pem", 200, now, "cert")
        plan = build_plan((root,), self.tmp / "Shelf", days=90, now=now)
        self.assertEqual([item.path.name for item in plan.cold[0].regenerable], ["myenv"])
        self.assertEqual(plan.cold[0].secrets, ())
        report = apply_plan(plan, yes=True)
        self.assertEqual(report.failures, ())
        archive = next((self.tmp / "Shelf").glob("*.tar.gz"))
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        self.assertIn("payload/keep.txt", names)
        self.assertFalse(any("myenv" in name for name in names))

    def test_protected_names_are_not_archived(self) -> None:
        now = time.time()
        root = self.tmp / "work"
        write_aged(root / "PasswordDB" / "db.txt", 400, now, "nope")
        write_aged(root / "Recovery Codes" / "codes.txt", 400, now, "nope")
        plan = build_plan((root,), self.tmp / "Shelf", days=90, now=now)
        self.assertEqual(plan.cold, ())
        reasons = {unit.path.name: unit.reason for unit in plan.denied}
        self.assertIn("PasswordDB", reasons)
        self.assertIn("Recovery Codes", reasons)

    def test_secret_inside_project_is_listed_and_archived(self) -> None:
        now = time.time()
        root, dest = self._sample(now)
        plan = build_plan((root,), dest, days=90, now=now)
        old = next(unit for unit in plan.cold if unit.path.name == "oldproj")
        self.assertIn(".env", old.secrets)
        report = apply_plan(plan, yes=True)
        self.assertEqual(report.failures, ())
        archive = next(dest.glob("*.tar.gz"))
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        self.assertIn("payload/.env", names)
        self.assertIn("payload/keep.txt", names)
        self.assertFalse(any("venv" in name for name in names))

    def test_apply_removes_cold_tree_and_warm_dependencies_only(self) -> None:
        now = time.time()
        root, dest = self._sample(now)
        plan = build_plan((root,), dest, days=90, now=now)
        apply_plan(plan, yes=True)
        self.assertFalse((root / "oldproj").exists())
        self.assertTrue((root / "oldproj.shelf.json").is_file())
        self.assertEqual((root / "newproj" / "keep.txt").read_text(), "fresh")
        self.assertFalse((root / "newproj" / "node_modules").exists())
        self.assertEqual((root / "PasswordDB" / "db.txt").read_text(), "secret")
        pointer = json.loads((root / "oldproj.shelf.json").read_text())
        archive = Path(pointer["archive"])
        self.assertTrue(archive.is_file())
        self.assertEqual(pointer["sha256"], hash_file(archive))

    def test_apply_without_yes_and_failed_archive_leave_files(self) -> None:
        now = time.time()
        root, dest = self._sample(now)
        plan = build_plan((root,), dest, days=90, now=now)
        with self.assertRaises(ShelfError):
            apply_plan(plan, yes=False)
        with patch("shelf.apply.create_archive", side_effect=ShelfError("boom")):
            with self.assertRaises(ShelfError):
                apply_plan(plan, yes=True)
        self.assertTrue((root / "oldproj" / "keep.txt").is_file())
        self.assertTrue((root / "newproj" / "node_modules" / "pkg.txt").is_file())
        self.assertEqual(list(dest.glob("*.tar.gz")), [])

    def test_restore_roundtrip_omits_dependencies(self) -> None:
        now = time.time()
        root, dest = self._sample(now)
        plan = build_plan((root,), dest, days=90, now=now)
        apply_plan(plan, yes=True)
        archive = next(dest.glob("*.tar.gz"))
        restored = restore_archive(archive)
        self.assertEqual(restored, root / "oldproj")
        self.assertEqual((restored / "keep.txt").read_text(), "keep")
        self.assertEqual((restored / ".env").read_text(), "TOKEN=example\n")
        self.assertFalse((restored / "venv").exists())

    def test_verified_pointer_removes_local_copy_without_a_second_archive(self) -> None:
        now = time.time()
        root, dest = self._sample(now)
        plan = build_plan((root,), dest, days=90, now=now)
        apply_plan(plan, yes=True)
        write_aged(root / "oldproj" / "keep.txt", 200, now, "keep")
        again = build_plan((root,), dest, days=90, now=now)
        pending = [unit for unit in again.cold if unit.path.name == "oldproj"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].action, "pending")
        apply_plan(again, yes=True)
        self.assertFalse((root / "oldproj").exists())
        self.assertEqual(len(list(dest.glob("*.tar.gz"))), 1)

    def test_used_folder_is_not_removed_when_pointer_is_stale(self) -> None:
        now = time.time()
        root, dest = self._sample(now)
        plan = build_plan((root,), dest, days=90, now=now)
        apply_plan(plan, yes=True)
        write_aged(root / "oldproj" / "keep.txt", 1, now, "back")
        again = build_plan((root,), dest, days=90, now=now)
        self.assertTrue(any("used since" in warning for warning in again.warnings))
        apply_plan(again, yes=True)
        self.assertEqual((root / "oldproj" / "keep.txt").read_text(), "back")

    def test_verify_rejects_a_tampered_manifest(self) -> None:
        now = time.time()
        root, dest = self._sample(now)
        plan = build_plan((root,), dest, days=90, now=now)
        unit = next(item for item in plan.cold if item.path.name == "oldproj")
        built = create_archive(unit, progress=False)
        try:
            manifest = json.loads(built.manifest_path.read_text(encoding="utf-8"))
            manifest["files"][0]["sha256"] = "0" * 64
            with self.assertRaises(ShelfError):
                verify_archive(built.tar_path, manifest)
        finally:
            discard_work(built)

    def test_payload_relative_rejects_parent_segments(self) -> None:
        with self.assertRaises(ShelfError):
            payload_relative("payload/../secret")

    def test_remove_refuses_paths_outside_roots_and_over_destination(self) -> None:
        outside = self.tmp / "outside.txt"
        outside.write_text("keep")
        root = self.tmp / "root"
        project = root / "proj"
        project.mkdir(parents=True)
        (project / "a.txt").write_text("a")
        with self.assertRaises(ShelfError):
            remove_local(outside, (root,), self.tmp / "Shelf")
        self.assertTrue(outside.is_file())
        destination = project / "Shelf"
        destination.mkdir()
        with self.assertRaises(ShelfError):
            remove_local(project, (root,), destination)
        self.assertTrue((project / "a.txt").is_file())

    def test_days_must_be_positive(self) -> None:
        with self.assertRaises(ShelfError):
            build_plan((self.tmp,), self.tmp / "Shelf", days=0)

    def test_plan_command_is_read_only(self) -> None:
        now = time.time()
        root, dest = self._sample(now)
        stdout = StringIO()
        with redirect_stdout(stdout), redirect_stderr(StringIO()):
            code = main(["plan", "--root", str(root), "--dest", str(dest), "--days", "90"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing was changed.", stdout.getvalue())
        self.assertIn("oldproj", stdout.getvalue())
        self.assertTrue((root / "oldproj" / "keep.txt").is_file())
        self.assertFalse(dest.exists())

    def test_apply_command_requires_yes(self) -> None:
        root, dest = self._sample(time.time())
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            code = main(["apply", "--root", str(root), "--dest", str(dest)])
        self.assertEqual(code, 1)
        self.assertIn("--yes", stderr.getvalue())
        self.assertTrue((root / "oldproj" / "keep.txt").is_file())
        self.assertTrue((root / "newproj" / "node_modules").exists())

    def test_json_plan_lists_cold_path(self) -> None:
        root, dest = self._sample(time.time())
        stdout = StringIO()
        with redirect_stdout(stdout), redirect_stderr(StringIO()):
            code = main(["plan", "--root", str(root), "--dest", str(dest), "--json"])
        self.assertEqual(code, 0)
        document = json.loads(stdout.getvalue())
        paths = [item["path"] for item in document["cold"]]
        self.assertTrue(any(path.endswith("oldproj") for path in paths))

    def test_small_old_file_is_ignored(self) -> None:
        now = time.time()
        root = self.tmp / "work"
        write_aged(root / "note.txt", 200, now, "tiny")
        blob = root / "blob.bin"
        write_aged(blob, 200, now, "")
        blob.write_bytes(b"a" * MIN_FILE_BYTES)
        stamp = now - 200 * 86400
        os.utime(blob, (stamp, stamp))
        plan = build_plan((root,), self.tmp / "Shelf", days=90, now=now)
        names = {unit.path.name for unit in plan.cold}
        self.assertEqual(names, {"blob.bin"})

    def _sample(self, now: float) -> tuple[Path, Path]:
        root = self.tmp / "work"
        dest = self.tmp / "Shelf"
        write_aged(root / "oldproj" / "keep.txt", 200, now, "keep")
        write_aged(root / "oldproj" / ".env", 200, now, "TOKEN=example\n")
        write_aged(root / "oldproj" / "venv" / "pkg.txt", 1, now, "wheel")
        write_aged(root / "newproj" / "keep.txt", 1, now, "fresh")
        write_aged(root / "newproj" / "node_modules" / "pkg.txt", 1, now, "mod")
        write_aged(root / "PasswordDB" / "db.txt", 400, now, "secret")
        return root, dest
