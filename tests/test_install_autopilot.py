import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest import mock
from agent_pooper_scooper import autopilot as ap
from tools import install_autopilot as install


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.agents = self.base / 'LaunchAgents'
        self.agents.mkdir()
        self.exe = self.base / 'aps'
        self.exe.write_text('#!/bin/sh\nprintf "aps 0.2.0a1\\n"\n')
        self.exe.chmod(0o700)
        self.state = str(self.base / 'policy')
        ap.initialize(self.state)

    def run_install(self, **kwargs):
        return install.install(str(self.exe), self.state, str(self.agents), no_start=True, **kwargs)

    def test_dedicated_interval_absolute_program_idempotent_preserves_monitor(self):
        monitor = self.agents / 'io.agent-pooper-scooper.monitor.plist'
        monitor.write_bytes(b'existing monitor')
        first = self.run_install()
        path = self.agents / (install.LABEL + '.plist')
        value = plistlib.loads(path.read_bytes())
        self.assertEqual(value['StartInterval'], 60)
        self.assertEqual(value['Nice'], 10)
        self.assertTrue(value['RunAtLoad'])
        self.assertEqual(value['ProgramArguments'], [str(self.exe), 'autopilot', 'tick', '--state-dir', self.state])
        self.assertEqual(value['Label'], 'io.agent-pooper-scooper.autopilot')
        self.assertEqual(self.run_install()['status'], 'already_installed')
        self.assertEqual(monitor.read_bytes(), b'existing monitor')
        self.assertNotIn(self.state, str(first))

    def test_unknown_destination_symlink_nonabsolute_and_unsafe_state_refused(self):
        with self.assertRaises(Exception):
            install.install('aps', self.state, str(self.agents), no_start=True)
        path = self.agents / (install.LABEL + '.plist')
        path.write_bytes(b'unknown')
        with self.assertRaises(Exception):
            self.run_install()
        self.assertEqual(path.read_bytes(), b'unknown')
        path.unlink()
        path.symlink_to(self.exe)
        with self.assertRaises(Exception):
            self.run_install()
        self.assertTrue(path.is_symlink())

    def test_version_and_permissions_checked_before_writing(self):
        self.exe.chmod(0o777)
        with self.assertRaises(Exception):
            self.run_install()
        self.assertEqual(list(self.agents.iterdir()), [])

    def test_service_no_shell_only_explicit_start(self):
        with mock.patch.object(install.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'aps 0.2.0a1\n'
            self.run_install()
            self.assertEqual(run.call_count, 1)
            install.install(str(self.exe), self.state, str(self.agents), no_start=False, platform='darwin')
            self.assertEqual(run.call_args.args[0][0:2], ['/bin/launchctl', 'bootstrap'])
