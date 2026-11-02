"""First install refuses existing/native data before it creates anything."""
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock
import zipfile

from tools import install_macos as installer


def make_wheel(directory, *, version="0.1.0a1", metadata_version=None):
    wheel = directory / f"agent_pooper_scooper-{version}-py3-none-any.whl"
    info = "agent_pooper_scooper-" + version + ".dist-info"
    members = {
        "agent_pooper_scooper/__init__.py": "",
        "agent_pooper_scooper/cli.py": "def main():\n    print('aps " + version + "')\n",
        info + "/METADATA": "Metadata-Version: 2.1\nName: agent-pooper-scooper\nVersion: " + (metadata_version or version) + "\n",
        info + "/WHEEL": "Wheel-Version: 1.0\nGenerator: test-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        info + "/entry_points.txt": "[console_scripts]\naps = agent_pooper_scooper.cli:main\n",
    }
    members[info + "/RECORD"] = "".join(name + ",,\n" for name in members) + info + "/RECORD,,\n"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "owned home with ' quote"
        self.home.mkdir()
        self.wheel, self.digest = make_wheel(self.base)
        self.prefix = self.home / ".local" / "share" / "agent-pooper-scooper"

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, **options):
        return installer.plan_install(self.wheel, self.digest, home=self.home, **options)

    def test_dry_run_validates_without_writes_or_subprocesses(self):
        before = set(self.home.rglob("*"))
        output = io.StringIO()
        with mock.patch.object(installer.Path, "home", return_value=self.home), mock.patch.object(installer.subprocess, "run", side_effect=AssertionError("must not launch")), mock.patch.object(installer.venv.EnvBuilder, "create", side_effect=AssertionError("must not build")), redirect_stdout(output):
            code = installer.main(["--wheel", str(self.wheel), "--sha256", self.digest, "--dry-run", "--no-start"])
        self.assertEqual(code, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["status"], "planned")
        self.assertFalse(plan["start_requested"])
        self.assertEqual(plan["version"], "0.1.0a1")
        self.assertEqual(set(self.home.rglob("*")), before)
        self.assertFalse(self.prefix.exists())

    def test_all_existing_destinations_refused_before_any_writes(self):
        for relative in (".local/share/agent-pooper-scooper", ".local/bin/aps",
                         "Desktop/Agent-Pooper-Scooper.command",
                         "Library/LaunchAgents/" + installer.LABEL + ".plist"):
            with self.subTest(relative=relative):
                separate = self.base / ("case-" + str(len(list(self.base.iterdir()))))
                separate.mkdir()
                target = separate / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"unknown active/resumable/native data")
                with self.assertRaises(installer.InstallError):
                    installer.plan_install(self.wheel, self.digest, home=separate)
                self.assertEqual(target.read_bytes(), b"unknown active/resumable/native data")
                self.assertFalse((separate / ".local/share/agent-pooper-scooper/releases").exists())

    def test_symlink_ancestor_does_not_receive_files(self):
        outside = self.base / "unknown-worktree"
        outside.mkdir()
        sentinel = outside / "uncommitted.py"
        sentinel.write_bytes(b"keep original")
        (self.home / ".local").symlink_to(outside)
        with self.assertRaisesRegex(installer.InstallError, "nonphysical"):
            self.plan()
        self.assertEqual(list(outside.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"keep original")

    def test_existing_symlink_destination_is_not_followed(self):
        (self.home / "Desktop").mkdir()
        sentinel = self.base / "session-history"
        sentinel.write_bytes(b"resumable")
        (self.home / "Desktop/Agent-Pooper-Scooper.command").symlink_to(sentinel)
        with self.assertRaises(installer.InstallError):
            self.plan()
        self.assertEqual(sentinel.read_bytes(), b"resumable")
        self.assertFalse(self.prefix.exists())

    def test_wrong_digest_refuses_before_writes(self):
        with self.assertRaisesRegex(installer.InstallError, "sha256"):
            installer.plan_install(self.wheel, "0" * 64, home=self.home)
        self.assertFalse(self.prefix.exists())

    def test_metadata_version_mismatch_and_unsafe_filename_refused(self):
        wheel, digest = make_wheel(self.base, version="9.9.9", metadata_version="1.2.3")
        with self.assertRaisesRegex(installer.InstallError, "metadata"):
            installer.plan_install(wheel, digest, home=self.home)
        named = self.base / "other-package-1.0.0-py3-none-any.whl"
        named.write_bytes(wheel.read_bytes())
        with self.assertRaisesRegex(installer.InstallError, "wheel_name"):
            installer.plan_install(named, digest, home=self.home)

    def test_archive_escape_refused(self):
        with zipfile.ZipFile(self.wheel, "a") as archive:
            archive.writestr("../unknown-native-file", "do not extract")
        digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        with self.assertRaisesRegex(installer.InstallError, "unsafe_wheel_member"):
            installer.plan_install(self.wheel, digest, home=self.home)
        self.assertFalse(self.prefix.exists())

    def test_changed_wheel_or_destination_between_plan_and_install_refused(self):
        plan = self.plan(no_start=True)
        (self.home / "Desktop").mkdir()
        target = self.home / "Desktop/Agent-Pooper-Scooper.command"
        target.write_bytes(b"arrived after preflight")
        with self.assertRaises(installer.InstallError):
            installer.install(plan, platform="darwin")
        self.assertEqual(target.read_bytes(), b"arrived after preflight")
        self.assertFalse(self.prefix.exists())

    def test_changed_wheel_and_edited_program_plan_refused(self):
        plan = self.plan(no_start=True)
        plan["program_arguments"].append("unexpected-command")
        with self.assertRaisesRegex(installer.InstallError, "plan_changed"):
            installer.install(plan, platform="darwin")
        plan = self.plan(no_start=True)
        with self.wheel.open("ab") as stream:
            stream.write(b"changed bytes")
        with self.assertRaisesRegex(installer.InstallError, "sha256"):
            installer.install(plan, platform="darwin")
        self.assertFalse(self.prefix.exists())

    def test_registration_runs_bootstrap_then_read_only_inspection(self):
        plan = self.plan()
        commands = []

        class Builder:
            def create(self, destination):
                bindir = Path(destination) / "bin"
                bindir.mkdir()
                (bindir / "aps").write_text("fixture installed entrypoint")

        def runner(argv, **kwargs):
            commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="aps 0.1.0a1" if argv[-1] == "--version" else "", stderr="")

        result = installer.install(plan, platform="darwin", runner=runner, builder=Builder())
        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["service_status"], "registered")
        domain = "gui/" + str(os.getuid())
        self.assertEqual(commands[-2], ["/bin/launchctl", "bootstrap", domain, plan["paths"]["plist"]])
        self.assertEqual(commands[-1], ["/bin/launchctl", "print", domain + "/" + installer.LABEL])
        self.assertEqual(json.loads(Path(plan["paths"]["receipt"]).read_text())["service_status"], "registered")

    def test_first_install_fresh_fixture_offline_preserves_native_data(self):
        sentinel = self.home / "resumable-session.jsonl"
        sentinel.write_bytes(b"preserve original bytes")
        home_mode = stat.S_IMODE(self.home.stat().st_mode)
        plan = self.plan(no_start=True)
        commands = []

        def runner(argv, **kwargs):
            commands.append(argv)
            self.assertNotIn("launchctl", argv[0])
            return subprocess.run(argv, **kwargs)

        result = installer.install(plan, platform="darwin", runner=runner)
        paths = {key: Path(value) for key, value in result["paths"].items()}
        self.assertEqual(result["status"], "installed-not-started")
        self.assertEqual(sentinel.read_bytes(), b"preserve original bytes")
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), home_mode)
        self.assertEqual(paths["wrapper"].readlink(), paths["aps"])
        self.assertEqual(stat.S_IMODE(paths["shortcut"].stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths["plist"].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths["error_log"].stat().st_mode), 0o600)
        shortcut = paths["shortcut"].read_text().splitlines()[1]
        self.assertEqual(shlex.split(shortcut), ["exec", str(paths["aps"]), "monitor", "tick",
                                               "--state-dir", str(paths["state_dir"]), "--format", "markdown"])
        configuration = plistlib.loads(paths["plist"].read_bytes())
        self.assertEqual(configuration["ProgramArguments"], plan["program_arguments"])
        self.assertEqual(configuration["StartInterval"], 900)
        self.assertTrue(configuration["RunAtLoad"])
        self.assertEqual(configuration["StandardOutPath"], "/dev/null")
        self.assertEqual(json.loads(paths["receipt"].read_text())["status"], "installed-not-started")
        self.assertIn("--no-index", commands[0])
        self.assertIn("--no-deps", commands[0])
        self.assertIn("--isolated", commands[0])
        self.assertFalse(paths["state_dir"].exists(), "monitor owns its state initialization")
        with self.assertRaises(installer.InstallError):
            installer.install(plan, platform="darwin", runner=runner)


if __name__ == "__main__":
    unittest.main()
