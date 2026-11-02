import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from agent_pooper_scooper import autopilot as ap


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.state = str(self.base / 'policy')

    def test_default_disabled_retention_and_create_only(self):
        ap.initialize(self.state)
        value = ap.status(self.state)
        self.assertFalse(value['enabled'])
        self.assertEqual(value['retention_seconds'], 86400)
        ap.set_enabled(self.state, True)
        self.assertTrue(ap.status(self.state)['enabled'])
        with self.assertRaises(Exception):
            ap.initialize(self.state)

    def test_no_adoption_and_unknown_files_preserved(self):
        Path(self.state).mkdir()
        canary = Path(self.state) / 'session.json'
        canary.write_text('resume')
        with self.assertRaises(Exception):
            ap.initialize(self.state)
        self.assertEqual(canary.read_text(), 'resume')

    def test_strict_policy_numbers(self):
        for value in (True, 0, -1, float('nan'), float('inf'), 31536001):
            with self.assertRaises(Exception):
                ap.initialize(self.state, retention_seconds=value)
        self.assertFalse(Path(self.state).exists())

    def test_schema_tampering_and_duplicate_keys(self):
        ap.initialize(self.state)
        target = Path(self.state) / 'state.json'
        original = target.read_text()
        for text in (original.replace('86400', 'true'), original[:-2] + ', "unknown":1}', '{"schema_version":1,"schema_version":1}'):
            target.write_text(text)
            with self.assertRaises(Exception):
                ap.status(self.state)
        target.write_text(original)

    def test_symlink_hardlink_and_lock_contention(self):
        ap.initialize(self.state)
        link = self.base / 'alias'
        link.symlink_to(self.state)
        with self.assertRaises(Exception):
            ap.status(str(link))
        lock = Path(self.state) / 'lock'
        with lock.open('rb') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Exception):
                ap.set_enabled(self.state, True)
        os.link(Path(self.state) / 'state.json', self.base / 'extra')
        with self.assertRaises(Exception):
            ap.status(self.state)

    def test_replaced_runs_directory_refused(self):
        ap.initialize(self.state)
        runs = Path(self.state) / 'runs'
        runs.rename(Path(self.state) / 'old-runs')
        runs.mkdir(mode=0o700)
        with self.assertRaises(Exception):
            ap.status(self.state)

    def test_relative_unnormalized_and_marker_bool_refused(self):
        ap.initialize(self.state)
        for path in (os.path.relpath(self.state), self.state + '/../policy'):
            with self.assertRaises(Exception):
                ap.status(path)
        marker = Path(self.state) / 'marker.json'
        marker.write_text(marker.read_text().replace('"schema_version": 1', '"schema_version": true'))
        with self.assertRaises(Exception):
            ap.status(self.state)

    def test_oversize_state_and_unknown_staging_never_erased(self):
        ap.initialize(self.state)
        stage = Path(self.state) / 'stage-unknown'
        stage.write_bytes(b'uncertain')
        with self.assertRaises(Exception):
            ap.set_enabled(self.state, True)
        self.assertEqual(stage.read_bytes(), b'uncertain')
        stage.unlink()  # This test's synthetic canary only.
        target = Path(self.state) / 'state.json'
        target.write_bytes(b'x' * (ap.MAX_STATE + 1))
        with self.assertRaises(Exception):
            ap.status(self.state)

    def test_init_refuses_nonphysical_spelling_before_any_creation(self):
        for spelling in (os.path.relpath(self.state), self.state + '/', str(self.base / 'unused' / '..' / 'policy')):
            with self.assertRaises(Exception):
                ap.initialize(spelling)
            self.assertFalse(Path(self.state).exists())
