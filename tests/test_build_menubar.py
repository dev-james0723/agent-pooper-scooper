import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import hashlib
import json
from unittest import mock
import sys
import tempfile
import unittest

from tools import build_menubar


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        source = Path(__file__).resolve().parents[1]
        # Publication tests run immutable source snapshots without VCS metadata.
        # Assembly still requires real provenance; provide our own Git fixture
        # rather than depending on the checkout containing this test module.
        self.repo = self.base / "source-checkout"
        (self.repo / "src").mkdir(parents=True)
        shutil.copytree(source / "src/agent_pooper_scooper",
                        self.repo / "src/agent_pooper_scooper",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_AUTHOR_NAME": "Fixture", "GIT_COMMITTER_NAME": "Fixture",
                    "GIT_AUTHOR_EMAIL": "fixture@users.noreply.github.com",
                    "GIT_COMMITTER_EMAIL": "fixture@users.noreply.github.com"})
        for args in (("init", "-q", "-b", "main"), ("add", "src"),
                     ("commit", "-qm", "synthetic engine fixture")):
            subprocess.run(["git", *args], cwd=self.repo, env=env, check=True,
                           capture_output=True)
        self.source_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, env=env, text=True).strip()
        self.binary = self.base / "APSMenuBar"
        self.binary.write_bytes(bytes.fromhex("cffaedfe") + b"fixture executable")
        self.binary.chmod(0o700)
        self.output = self.base / "Agent Pooper Scooper.app"

    def test_bundle_only_contains_engine_sources_and_private_runtime_is_excluded(self):
        result = build_menubar.assemble(self.repo, self.binary, self.output, Path(sys.executable), sign=False)
        info = plistlib.loads((self.output / "Contents/Info.plist").read_bytes())
        self.assertTrue(info["LSUIElement"])
        self.assertEqual(info["CFBundleShortVersionString"], "0.2.0")
        self.assertEqual(info["CFBundleVersion"], "2")
        self.assertEqual(info["LSMinimumSystemVersion"], "13.0")
        resources = self.output / "Contents/Resources"
        self.assertTrue((resources / "aps-menu").stat().st_mode & 0o100)
        self.assertTrue((resources / "engine/agent_pooper_scooper/menubar.py").is_file())
        self.assertFalse(list(resources.rglob("config.json")))
        self.assertFalse(list(resources.rglob("owner.json")))
        self.assertTrue(result["source_sha256"])
        self.assertEqual(result["engine_source_commit"], self.source_commit)

    def test_detached_receipt_hashes_final_signed_bytes(self):
        original = build_menubar.subprocess.run
        def signing(command, *args, **kwargs):
            if command[0] == "/usr/bin/codesign":
                if "--sign" in command:
                    path = self.output / "Contents/MacOS/APSMenuBar"
                    path.write_bytes(path.read_bytes() + b"changed-by-signing")
                return build_menubar.subprocess.CompletedProcess(command, 0)
            return original(command, *args, **kwargs)
        with mock.patch.object(build_menubar.subprocess, "run", side_effect=signing):
            result = build_menubar.assemble(self.repo, self.binary, self.output, Path(sys.executable), sign=True)
        actual = hashlib.sha256((self.output / "Contents/MacOS/APSMenuBar").read_bytes()).hexdigest()
        self.assertEqual(result["binary_sha256"], actual)
        receipt = json.loads(Path(str(self.output) + ".receipt.json").read_text())
        self.assertEqual(receipt["binary_sha256"], actual)

    def test_supplied_binary_has_no_native_source_attestation(self):
        result = build_menubar.assemble(self.repo, self.binary, self.output, Path(sys.executable), sign=False)
        self.assertIsNone(result["native_source_commit"])
        self.assertEqual(result["native_provenance"], "unverified-input")

    def test_existing_destination_is_never_overwritten(self):
        self.output.mkdir()
        keep = self.output / "user-document"
        keep.write_text("keep")
        with self.assertRaises((ValueError, FileExistsError)):
            build_menubar.assemble(self.repo, self.binary, self.output, Path(sys.executable), sign=False)
        self.assertEqual(keep.read_text(), "keep")

    def test_symlink_destination_and_parent_refuse(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.output.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((ValueError, FileExistsError)):
            build_menubar.assemble(self.repo, self.binary, self.output, Path(sys.executable), sign=False)
        self.assertEqual(list(outside.iterdir()), [])
        with self.assertRaises(ValueError):
            build_menubar.assemble(self.repo, self.binary, self.output / "nested.app", Path(sys.executable), sign=False)
        self.assertEqual(list(outside.iterdir()), [])

    def test_non_macho_binary_is_rejected_before_output(self):
        self.binary.write_bytes(b"not executable")
        with self.assertRaises(ValueError):
            build_menubar.assemble(self.repo, self.binary, self.output, Path(sys.executable), sign=False)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
