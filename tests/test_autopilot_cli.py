import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from agent_pooper_scooper import cli, autopilot


class AutopilotCLITests(unittest.TestCase):
    def call(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['autopilot', *args])
        return code, json.loads(out.getvalue())

    def test_init_status_controls_and_public_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp).resolve() / 'policy')
            code, value = self.call('init', '--state-dir', path)
            self.assertEqual(code, 0)
            self.assertEqual(value['retention_seconds'], 86400)
            self.assertNotIn(temp, str(value))
            self.assertTrue(self.call('enable', '--state-dir', path)[1]['enabled'])
            self.assertFalse(self.call('disable', '--state-dir', path)[1]['enabled'])
            code, value = self.call('hold', '--state-dir', path, '--enrollment-id', '../../private')
            self.assertEqual(code, 1)
            self.assertNotIn('../../private', str(value))

    def test_literal_argv_and_output_not_public(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp).resolve() / 'policy')
            self.call('init', '--state-dir', path)
            receipt = {'enrollment_id': 'a'*32, 'command': {'exit_code': 0}, 'capture': {'stored_bytes': 4, 'complete': True}, 'cleanup': {'status': 'deferred'}}
            command = [sys.executable, '-c', 'print("secret")', 'a b', '$(not-a-shell)']
            with mock.patch('agent_pooper_scooper.autopilot_tasks.run', return_value=(receipt, 0)) as run:
                code, value = self.call('run', '--state-dir', path, '--disposable-output', '--', *command)
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args[1], command)
            self.assertTrue(run.call_args.kwargs['disposable_output'])
            self.assertNotIn('secret', str(value))

    def test_command_success_never_hides_capture_failure(self):
        from autopilot_support import inprocess_broker
        with tempfile.TemporaryDirectory() as temp, inprocess_broker():
            path = str(Path(temp).resolve() / 'policy')
            self.call('init', '--state-dir', path)
            with mock.patch('agent_pooper_scooper.runner._capture', side_effect=RuntimeError('secret')):
                code, value = self.call('run', '--state-dir', path, '--', sys.executable, '-c', 'pass')
            self.assertNotEqual(code, 0)
            self.assertNotIn('secret', str(value))
