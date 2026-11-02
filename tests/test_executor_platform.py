"""Native mount observations exclude hazards; they are not kernel containment."""
import os
from pathlib import Path
import struct
import tempfile
import unittest

from agent_pooper_scooper import executor_platform as platform_guard


class PlatformObservations(unittest.TestCase):
    def test_mountinfo_escaped_names_and_distinct_prefixes(self):
        rows = platform_guard._parse_mountinfo(
            "12 1 8:1 / / rw,relatime shared:1 - ext4 /dev/fixture rw\n"
            "13 12 8:1 / /scratch\\040space rw - ext4 /dev/fixture rw\n")
        self.assertEqual(rows[13]["mountpoint"], "/scratch space")
        platform_guard._no_nested_mounts("/scratch", ["/scratch-extra"])
        with self.assertRaises(platform_guard.PlatformUnavailable):
            platform_guard._no_nested_mounts("/scratch", ["/scratch/file"])

    def test_mountinfo_malformed_duplicate_or_unknown_fields_refuse(self):
        valid = "12 1 8:1 / / rw - ext4 /dev/fixture rw\n"
        for text in ("", "nonsense", valid + valid,
                     valid.replace(" rw - ", " rw invented:1 - "),
                     valid.replace(" / rw", " /bad\\999path rw"),
                     valid.replace(" ext4 ", " ext4 extra ")):
            with self.subTest(text=text), self.assertRaises(platform_guard.PlatformUnavailable):
                platform_guard._parse_mountinfo(text)

    def test_darwin_record_layout_and_terminated_strings(self):
        raw = bytearray(2168)
        struct.pack_into("=ii", raw, 48, 7, 8)
        struct.pack_into("=I", raw, 64, 0x1000)
        raw[72:76] = b"apfs"
        raw[88:96] = b"/fixture"
        record = platform_guard._darwin_record(bytes(raw))
        self.assertEqual(record["fsid"], (7, 8))
        self.assertEqual(record["fstype"], "apfs")
        platform_guard._darwin_local(record)
        for altered in (bytes(raw[:-1]), bytes(raw[:72] + b"x" * 16 + raw[88:]),
                        bytes(raw[:88] + b"x" * 1024 + raw[1112:])):
            with self.assertRaises(platform_guard.PlatformUnavailable):
                platform_guard._darwin_record(altered)

    def test_nonlocal_readonly_and_unknown_filesystems_refuse(self):
        for fstype, flags in (("apfs", 0), ("apfs", 0x1001), ("smbfs", 0x1000),
                              ("fusefs", 0x1000), ("unknown", 0x1000)):
            with self.subTest(fstype=fstype, flags=flags), self.assertRaises(platform_guard.PlatformUnavailable):
                platform_guard._darwin_local({"fstype": fstype, "flags": flags})

    def test_owned_fixture_native_observation_and_namespace_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                state = platform_guard.observe(fd, str(root))
                self.assertIn(state["backend"], ("darwin-statfs64", "linux-mountinfo"))
                self.assertEqual(state["contract"], "closed-coordinated-store")
                platform_guard.sync_directory(fd)
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
