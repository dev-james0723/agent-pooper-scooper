import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from agent_pooper_scooper import autopilot as ap, autopilot_tasks as tasks, runner
from agent_pooper_scooper.authority import ManagedAuthority
from agent_pooper_scooper.managed_cli import _capability


from autopilot_support import inprocess_broker


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.transport = inprocess_broker()
        self.transport.__enter__()
        self.addCleanup(self.transport.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.state = str(self.base / 'policy')
        ap.initialize(self.state, retention_seconds=1, enabled=True)

    def run_task(self, source="print('synthetic')", disposable=True, **kwargs):
        return tasks.run(self.state, [sys.executable, '-c', source], disposable_output=disposable, **kwargs)

    def row(self):
        with ap._state(self.state) as (_, data):
            return dict(data['runs'][-1])

    def run_path(self):
        return Path(self.state) / 'runs' / self.row()['id']

    def owner(self):
        path = self.run_path()
        return ManagedAuthority(str(path / 'store'), owner_key=_capability(str(path / 'owner.json'), 'owner'))

    def test_pending_enrollment_before_command_and_retention(self):
        original = runner.run_command
        def inspect(*a, **kw):
            self.assertEqual(self.row()['status'], 'pending')
            return original(*a, **kw)
        with mock.patch.object(runner, 'run_command', side_effect=inspect):
            receipt, code = self.run_task()
        self.assertEqual(code, 0)
        self.assertEqual(self.row()['status'], 'success')
        self.assertEqual(self.row()['retention'], 1)
        self.assertEqual(receipt['cleanup']['status'], 'deferred')
        self.assertTrue(list((self.run_path() / 'store/artifacts').glob('*.blob')))
        self.assertEqual(self.owner().snapshot(runner._TASK)['grace_seconds'], 1)

    def test_default_failed_timeout_limit_never_eligible(self):
        cases = [("print('primary')", False, {}), ("print('failed'); exit(7)", True, {}),
                 ("import time; print('slow', flush=True); time.sleep(2)", True, {'timeout': .15}),
                 ("print('x'*10000)", True, {'max_output_bytes': 10})]
        for source, disposable, kw in cases:
            self.run_task(source, disposable, **kw)
            self.assertEqual(self.row()['status'], 'retained')
            self.assertTrue(list((self.run_path() / 'store/artifacts').glob('*.blob')))

    def test_incomplete_exception_retains_pending_no_replay(self):
        with mock.patch.object(runner, 'run_command', side_effect=RuntimeError):
            with self.assertRaises(Exception):
                self.run_task()
        self.assertEqual(self.row()['status'], 'pending')

    def test_no_foreign_root_registration_and_no_overwrite(self):
        self.assertFalse(hasattr(tasks, 'enroll_path'))
        sentinel = self.base / 'worktree'
        sentinel.mkdir()
        (sentinel / 'session').write_text('resume')
        with self.assertRaises(Exception):
            tasks.run(str(sentinel), [sys.executable, '-c', "print('bad')"], disposable_output=True)
        self.assertEqual((sentinel / 'session').read_text(), 'resume')

    def test_production_retention_and_bounds(self):
        other = str(self.base / 'production')
        ap.initialize(other)
        tasks.run(other, [sys.executable, '-c', "print('safe')"], disposable_output=True)
        with ap._state(other) as (_, data):
            self.assertEqual(data['runs'][0]['retention'], 86400)
        for value in (True, float('nan'), 0):
            with self.assertRaises(Exception):
                self.run_task(timeout=value)

    def test_saturation_refuses_without_touching_existing_state(self):
        import copy
        import uuid
        with mock.patch.object(runner, 'run_command', side_effect=RuntimeError):
            with self.assertRaises(Exception):
                self.run_task()
        with ap._state(self.state) as (fd, data):
            template = data['runs'][0]
            data['runs'] = [dict(copy.deepcopy(template), id=uuid.uuid4().hex) for _ in range(ap.MAX_RUNS)]
            ap._save(fd, data)
        with mock.patch.object(runner, 'run_command', side_effect=AssertionError('must not start')):
            with self.assertRaises(Exception):
                self.run_task()
        self.assertEqual(ap.status(self.state)['enrolled_runs'], 128)

    def test_hold_and_disable_during_capture_preserve_intent(self):
        original = runner._capture
        def capture(*args, **kwargs):
            ap.set_hold(self.state, self.row()['id'], True)
            ap.set_enabled(self.state, False)
            original(*args, **kwargs)
        with mock.patch.object(runner, '_capture', capture):
            self.run_task()
        self.assertTrue(self.row()['hold'])
        self.assertFalse(ap.status(self.state)['enabled'])
        self.assertEqual(self.row()['status'], 'success')
