"""Native inventory must remain bounded, honest and strictly read-only."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_pooper_scooper import disk, host_report, schema


class HostReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "private-project"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_native_source_sessions_and_unknown_data_remain_identical(self):
        targets = []
        for relative in ("worktree/uncommitted.py", "sessions/resumable.jsonl", "unknown.bin"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("preserve " + relative).encode())
            targets.append(path)
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino)
                  for path in targets}
        with mock.patch("builtins.open", side_effect=AssertionError("content read forbidden")):
            result = host_report.scan([self.root])
        for path, state in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino), state)
        self.assertEqual(result["totals"]["files"], 3)
        self.assertEqual(result["totals"]["apparent_bytes"], sum(len(v[0]) for v in before.values()))
        self.assertEqual(result["roots"][0]["decision"], "KEEP")
        self.assertEqual(result["cleanup_authority"], "none")
        self.assertEqual(result["task_attribution"], "unknown")
        self.assertEqual(result["totals"]["removed_bytes"], 0)
        self.assertIsNone(result["totals"]["reclaimed_bytes"])

    def test_defaults_only_existing_curated_roots_never_entire_home(self):
        for relative in (".claude/projects", ".codex/sessions", "Projects"):
            target = self.base / relative
            target.mkdir(parents=True)
            (target / "record").write_bytes(b"123")
        (self.base / "private-unrelated").write_bytes(b"unrelated content")
        real_expanduser = os.path.expanduser
        with mock.patch.object(host_report.os.path, "expanduser", side_effect=lambda p: str(self.base) if p == "~" else real_expanduser(p)):
            result = host_report.scan()
        self.assertEqual(result["totals"]["apparent_bytes"], 9)
        self.assertEqual(len(result["roots"]), 3)
        self.assertNotIn(str(self.base), [r["path"] for r in result["roots"]])
        self.assertEqual([r["category"] for r in result["roots"]], ["sessions", "sessions", "projects"])

    def test_bound_is_explicit_lower_bound(self):
        for n in range(20):
            (self.root / str(n)).write_bytes(b"12")
        result = host_report.scan([self.root], max_entries=3)
        self.assertLessEqual(result["totals"]["files"], 3)
        self.assertFalse(result["complete"])
        self.assertEqual(result["totals"]["measurement"], "known-lower-bound")
        self.assertIn("entry_limit", result["roots"][0]["usage"]["unknowns_by_reason"])

    def test_symlinks_and_ancestors_are_never_followed(self):
        outside = self.base / "outside-private"
        outside.mkdir()
        sentinel = outside / "keep-secret"
        sentinel.write_bytes(b"retained outside")
        (self.root / "link").symlink_to(outside)
        result = host_report.scan([self.root, self.root / "link", self.root / "link" / "keep-secret"])
        self.assertEqual(result["totals"]["apparent_bytes"], 0)
        self.assertFalse(result["complete"])
        self.assertEqual(result["roots"][1]["usage"]["root_status"], "symlink")
        self.assertEqual(result["roots"][2]["usage"]["root_status"], "symlink_in_path")
        self.assertEqual(sentinel.read_bytes(), b"retained outside")

    def test_mount_is_skipped_not_reported_as_measured_capacity(self):
        target = self.root / "external"
        target.mkdir()
        (target / "keep").write_bytes(b"mounted source")
        entry = SimpleNamespace(name="external", stat=lambda **kw: SimpleNamespace(
            st_mode=target.stat().st_mode, st_dev=self.root.stat().st_dev + 1))

        class Entries:
            def __enter__(self):
                return iter([entry])

            def __exit__(self, *args):
                return False

        with mock.patch.object(disk.os, "scandir", return_value=Entries()) as walk:
            result = host_report.scan([self.root])
        self.assertEqual(walk.call_count, 1)
        self.assertFalse(result["complete"])
        self.assertEqual(result["totals"]["files"], 0)
        self.assertEqual(result["roots"][0]["usage"]["skipped_by_reason"], {"mount_boundary": 1})
        self.assertEqual((target / "keep").read_bytes(), b"mounted source")

    def test_hardlinks_overlapping_roots_and_duplicates_count_once(self):
        inner = self.root / "nested"
        inner.mkdir()
        target = inner / "a"
        target.write_bytes(b"1234567")
        os.link(target, self.root / "other-name")
        result = host_report.scan([self.root, inner, self.root])
        self.assertEqual(len(result["roots"]), 2)
        self.assertEqual(result["totals"]["apparent_bytes"], 7)
        self.assertEqual(result["totals"]["files"], 1)

    def test_public_projection_has_no_paths_names_categories_or_details(self):
        (self.root / "private-filename").symlink_to(self.base / "private-destination")
        result = host_report.scan([self.root], public=True)
        rendered = json.dumps(result) + host_report.render_markdown(result)
        for sensitive in (str(self.base), "private-project", "private-filename", "private-destination", "native-data"):
            self.assertNotIn(sensitive, rendered)
        self.assertEqual(result["roots"][0]["bucket"], 1)
        self.assertNotIn("device", result["volumes"][0])

    def test_missing_root_is_explicit_unknown(self):
        result = host_report.scan([self.root / "missing"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["roots"][0]["usage"]["root_status"], "missing")
        self.assertEqual(result["totals"]["apparent_bytes"], 0)

    def test_pressure_does_not_walk_and_thresholds_are_meaningful(self):
        capacity = {"device": 1, "total_bytes": 1000, "available_bytes": 90, "free_bytes": 100}
        with mock.patch.object(disk, "walk_usage", side_effect=AssertionError("no scan")), mock.patch.object(disk, "volume_usage", return_value=capacity):
            low = host_report.pressure(self.root, min_free_bytes=0, min_free_percent=10)
            normal = host_report.pressure(self.root, min_free_bytes=0, min_free_percent=5)
        self.assertEqual(low["volumes"][0]["pressure"], "low")
        self.assertEqual(normal["volumes"][0]["pressure"], "normal")
        self.assertEqual(normal["volumes"][0]["available_percent"], 9)

    def test_pressure_read_failure_is_unknown_without_exception_details(self):
        with mock.patch.object(disk, "volume_usage", side_effect=PermissionError("private secret path")):
            result = host_report.pressure(self.root, public=True)
        self.assertFalse(result["complete"])
        self.assertEqual(result["unknown_count"], 1)
        self.assertNotIn("private", json.dumps(result))

    def test_validation_rejects_unbounded_or_nonfinite_inputs(self):
        for options in ({"max_entries": 0}, {"max_entries": 100_001}, {"max_depth": 65},
                        {"max_entries": True}, {"min_free_percent": float("nan")},
                        {"min_free_percent": float("inf")}, {"min_free_bytes": -1}):
            with self.subTest(options=options), self.assertRaises(schema.ValidationError):
                host_report.scan([self.root], **options)
        for roots in ([], [self.root] * 13, [Path.home()], ["/"]):
            with self.subTest(roots=roots), self.assertRaises(schema.ValidationError):
                host_report.scan(roots)


if __name__ == "__main__":
    unittest.main()
