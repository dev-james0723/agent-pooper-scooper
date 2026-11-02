"""Read-only disk telemetry: volume capacity and safe usage walks."""
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

from agent_pooper_scooper import disk  # noqa: E402


def write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def snapshot(root):
    """Names, sizes and mtimes of everything under root (no symlink following)."""
    state = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            state[full] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
    return state


class VolumeUsageTests(unittest.TestCase):
    def test_reports_capacity_for_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            usage = disk.volume_usage(tmp)
        self.assertGreater(usage["total_bytes"], 0)
        self.assertGreaterEqual(usage["total_bytes"], usage["free_bytes"])
        self.assertGreaterEqual(usage["free_bytes"], usage["available_bytes"])
        self.assertGreaterEqual(usage["available_bytes"], 0)
        self.assertIsInstance(usage["device"], int)


class WalkUsageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "root"
        self.root.mkdir()

    def tearDown(self):
        for dirpath, dirnames, _ in os.walk(self.base):
            for name in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o700)
                except OSError:
                    pass
        self._tmp.cleanup()

    def test_counts_regular_files(self):
        write(self.root / "a.bin", 1000)
        write(self.root / "sub" / "b.bin", 2500)
        result = disk.walk_usage(str(self.root))
        self.assertEqual(result.apparent_bytes, 3500)
        self.assertEqual(result.files, 2)
        self.assertEqual(result.directories, 2)
        self.assertGreater(result.allocated_bytes, 0)
        self.assertTrue(result.complete)
        self.assertEqual(result.unknowns, [])

    def test_single_file_root(self):
        target = write(self.root / "only.bin", 4096)
        result = disk.walk_usage(str(target))
        self.assertEqual((result.files, result.apparent_bytes), (1, 4096))

    def test_never_follows_symlinks(self):
        outside = self.base / "outside"
        write(outside / "big.bin", 50_000)
        write(self.root / "real.bin", 10)
        os.symlink(outside, self.root / "dirlink")
        os.symlink(outside / "big.bin", self.root / "filelink")
        result = disk.walk_usage(str(self.root))
        self.assertEqual(result.apparent_bytes, 10)
        reasons = {(Path(item["path"]).name, item["reason"]) for item in result.skipped}
        self.assertIn(("dirlink", "symlink"), reasons)
        self.assertIn(("filelink", "symlink"), reasons)

    def test_symlink_root_is_not_followed(self):
        outside = self.base / "outside"
        write(outside / "big.bin", 50_000)
        link = self.base / "link-root"
        os.symlink(outside, link)
        result = disk.walk_usage(str(link))
        self.assertEqual((result.files, result.apparent_bytes), (0, 0))
        self.assertEqual(result.skipped[0]["reason"], "symlink")

    def test_symlink_in_root_ancestors_is_not_followed(self):
        target = write(self.root / "nested" / "private.bin", 500)
        os.symlink(self.root, self.base / "alias")
        result = disk.walk_usage(str(self.base / "alias" / "nested"))
        self.assertEqual(result.apparent_bytes, 0)
        self.assertFalse(result.complete)
        self.assertEqual(result.root_status, "symlink_in_path")
        self.assertTrue(target.exists())

    def test_hardlinks_counted_once(self):
        original = write(self.root / "original.bin", 5000)
        os.link(original, self.root / "linked.bin")
        result = disk.walk_usage(str(self.root))
        self.assertEqual(result.apparent_bytes, 5000)
        self.assertEqual(result.files, 1)
        self.assertEqual(result.duplicates, 1)

    def test_shared_seen_set_dedupes_across_walks(self):
        original = write(self.root / "a" / "original.bin", 5000)
        (self.root / "b").mkdir()
        os.link(original, self.root / "b" / "linked.bin")
        seen = set()
        first = disk.walk_usage(str(self.root / "a"), seen=seen)
        second = disk.walk_usage(str(self.root / "b"), seen=seen)
        self.assertEqual(first.apparent_bytes + second.apparent_bytes, 5000)

    def test_nested_overlap_counted_once(self):
        write(self.root / "inner" / "f.bin", 700)
        seen = set()
        outer = disk.walk_usage(str(self.root), seen=seen)
        inner = disk.walk_usage(str(self.root / "inner"), seen=seen)
        self.assertEqual(outer.apparent_bytes, 700)
        self.assertEqual(inner.apparent_bytes, 0)
        self.assertEqual(inner.duplicates, 1)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_directory_recorded_as_unknown(self):
        write(self.root / "locked" / "secret.bin", 999)
        write(self.root / "open.bin", 1)
        os.chmod(self.root / "locked", 0)
        result = disk.walk_usage(str(self.root))
        self.assertEqual(result.apparent_bytes, 1)
        self.assertFalse(result.complete)
        self.assertEqual([(Path(u["path"]).name, u["reason"]) for u in result.unknowns],
                         [("locked", "unreadable")])

    def test_missing_root_is_unknown(self):
        result = disk.walk_usage(str(self.root / "nope"))
        self.assertEqual(result.unknowns[0]["reason"], "missing")
        self.assertFalse(result.complete)

    def test_depth_limit_is_unknown_not_silently_dropped(self):
        write(self.root / "a" / "b" / "deep.bin", 300)
        write(self.root / "a" / "shallow.bin", 20)
        result = disk.walk_usage(str(self.root), max_depth=1)
        self.assertEqual(result.apparent_bytes, 20)
        self.assertFalse(result.complete)
        self.assertEqual([u["reason"] for u in result.unknowns], ["depth_limit"])

    def test_entry_limit_bounds_the_walk(self):
        for index in range(10):
            write(self.root / f"f{index}.bin", 1)
        result = disk.walk_usage(str(self.root), max_entries=3)
        self.assertLessEqual(result.files, 3)
        self.assertFalse(result.complete)
        self.assertIn("entry_limit", [u["reason"] for u in result.unknowns])

    def test_detail_lists_are_capped_but_counts_are_exact(self):
        target = self.base / "t"
        target.mkdir()
        for index in range(disk.MAX_DETAILS + 5):
            os.symlink(target, self.root / f"l{index}")
        result = disk.walk_usage(str(self.root))
        self.assertEqual(len(result.skipped), disk.MAX_DETAILS)
        self.assertEqual(result.skipped_by_reason["symlink"], disk.MAX_DETAILS + 5)

    def test_walk_is_read_only(self):
        write(self.root / "a" / "f.bin", 10)
        os.symlink(self.base, self.root / "loop")
        before = snapshot(self.base)
        disk.walk_usage(str(self.root))
        self.assertEqual(before, snapshot(self.base))

    def test_stops_at_mount_boundaries(self):
        target = write(self.root / "mounted" / "hidden.bin", 500).parent
        entry = SimpleNamespace(name=target.name, stat=lambda **kw: SimpleNamespace(
            st_mode=os.lstat(target).st_mode, st_dev=os.lstat(self.root).st_dev + 1))

        class Entries:
            def __enter__(self):
                return iter([entry])

            def __exit__(self, *args):
                pass

        with mock.patch.object(disk.os, "scandir", return_value=Entries()) as scan:
            result = disk.walk_usage(str(self.root))
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(result.apparent_bytes, 0)
        self.assertEqual(result.skipped, [{"path": str(target), "reason": "mount_boundary"}])

    def test_expected_identity_mismatch_measures_nothing(self):
        target = write(self.root / "f.bin", 10)
        st = os.lstat(target)
        ok = disk.walk_usage(str(target), expect=(st.st_dev, st.st_ino))
        self.assertEqual((ok.root_status, ok.apparent_bytes), ("measured", 10))
        bad = disk.walk_usage(str(target), expect=(st.st_dev, st.st_ino + 1))
        self.assertEqual((bad.root_status, bad.apparent_bytes), ("identity_changed", 0))
        self.assertFalse(bad.complete)

    def test_as_dict_is_json_ready(self):
        write(self.root / "a.bin", 3)
        data = disk.walk_usage(str(self.root)).as_dict()
        for key in ("path", "allocated_bytes", "apparent_bytes", "files", "directories",
                    "duplicates", "complete", "skipped", "unknowns",
                    "skipped_by_reason", "unknowns_by_reason"):
            self.assertIn(key, data)


if __name__ == "__main__":
    unittest.main()
