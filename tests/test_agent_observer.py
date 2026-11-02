import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import io
import os
import unittest
from unittest import mock
from agent_pooper_scooper import agent_observer as observer


class ObserverTests(unittest.TestCase):
    def observe(self, raw):
        with mock.patch.object(observer, '_process_output', return_value=raw):
            return observer.observe_agents()

    def test_exact_executable_same_uid_and_redaction(self):
        uid = os.getuid()
        value = self.observe(f'{uid} 11 /opt/bin/codex\n{uid} 12 /opt/bin/claude\n{uid} 13 /tmp/not-codex\n{uid+1} 14 codex\n'.encode())
        self.assertEqual(value['providers'], [{'provider': 'claude', 'count': 1, 'confidence': 'executable-name'}, {'provider': 'codex', 'count': 1, 'confidence': 'executable-name'}])
        self.assertEqual(value['cleanup_authority'], 'none')
        self.assertNotIn('/opt', str(value))
        self.assertNotIn('11', str(value))

    def test_unknown_malformed_and_oversize_are_unknown(self):
        for raw in (b'bad', b'0 -1 codex', b'x' * (observer.MAX_OUTPUT + 1), b'\xff'):
            self.assertEqual(self.observe(raw)['status'], 'unknown')

    def test_timeout_unknown_never_empty_proof(self):
        with mock.patch.object(observer, '_process_output', side_effect=TimeoutError):
            self.assertEqual(observer.observe_agents()['status'], 'unknown')

    def test_no_argv_probe(self):
        with mock.patch.object(observer.subprocess, 'Popen', side_effect=OSError) as launch:
            observer.observe_agents()
        args = launch.call_args.args[0]
        self.assertIn('uid=,pid=,comm=', args)
        self.assertNotIn('args', str(args))
