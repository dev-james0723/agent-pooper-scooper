import tempfile
import unittest
from pathlib import Path

from tools.publication_gate import safe_relative_path, scan_file, scan_repository


class PublicationGateTests(unittest.TestCase):
    def test_public_paths(self):
        self.assertTrue(safe_relative_path("README.md"))
        self.assertTrue(safe_relative_path("docs/ROADMAP.md"))
        self.assertTrue(safe_relative_path(".github/workflows/ci.yml"))
        self.assertTrue(safe_relative_path("src/package/cleaner.py"))
        self.assertFalse(safe_relative_path("production.env"))
        self.assertFalse(safe_relative_path("misc/notes.txt"))
        self.assertFalse(safe_relative_path("src/../secrets/token"))
        self.assertFalse(safe_relative_path("tools/.ao/private.json"))
        self.assertFalse(safe_relative_path(".github/unsafe.yml"))

    def test_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "docs").mkdir()
            (root / "data").write_text("sensitive")
            (root / "docs" / "alias").symlink_to(root / "data")
            self.assertIn("symlink", " ".join(scan_file(root, "docs/alias")))

    def test_private_mac_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "docs").mkdir()
            (root / "docs" / "private.md").write_text(
                "/" + "Users/" + "namedaccount/" + "Library/secret"
            )
            self.assertTrue(any(
                "absolute-mac-home" in item
                for item in scan_file(root, "docs/private.md")
            ))

    def test_missing_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(scan_file(Path(d), "docs/missing.md"))

    def test_text_file_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "docs").mkdir()
            (root / "docs" / "safe.md").write_text("No secrets in this public guide.")
            self.assertEqual(scan_file(root, "docs/safe.md"), [])

    def test_workflow_rejects_privileged_trigger(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "ci.yml").write_text(
                "on:\n  " + "pull_request" + "_target:\n"
            )
            self.assertTrue(any(
                "risky privileged" in item
                for item in scan_file(root, ".github/workflows/ci.yml")
            ))

    def test_repo_enumeration_failure_closed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(scan_repository(Path(d)))


if __name__ == "__main__":
    unittest.main()
