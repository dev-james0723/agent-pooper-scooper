"""Explicit private command streaming must not change the sanitized default."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from agent_pooper_scooper import autopilot, autopilot_tasks, cli, run_cli
from autopilot_support import inprocess_broker


class StreamingTests(unittest.TestCase):
    def test_task_can_mirror_private_stdout_and_stderr_when_explicit(self):
        with tempfile.TemporaryDirectory() as tmp, inprocess_broker(), tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            state = str(Path(tmp).resolve()/'policy')
            autopilot.initialize(state)
            receipt, code = autopilot_tasks.run(state, [sys.executable,'-c',
                "import sys; print('diagnostic stdout'); print('diagnostic stderr', file=sys.stderr)"],
                disposable_output=True, stdout=out, stderr=err)
            out.seek(0);err.seek(0)
            self.assertEqual(code,0)
            self.assertEqual(out.read(),b'diagnostic stdout\n')
            self.assertEqual(err.read(),b'diagnostic stderr\n')
            self.assertEqual(receipt['cleanup']['status'],'deferred')
            self.assertEqual(receipt['capture']['stored_bytes'],36)

    def test_cli_stream_flag_passes_sinks_and_uses_bounded_stderr_summary(self):
        receipt={'enrollment_id':'a'*32,'command':{'exit_code':0},
                 'capture':{'stored_bytes':9,'complete':True},'cleanup':{'status':'deferred'}}
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             mock.patch.object(autopilot_tasks,'run',return_value=(receipt,0)) as run, \
             mock.patch.object(run_cli,'_summary') as summary:
            code=cli.main(['autopilot','run','--state-dir','/physical/state','--stream-output',
                           '--disposable-output','--','diagnostic','literal argument'])
        self.assertEqual(code,0)
        self.assertIs(run.call_args.kwargs['stdout'],out)
        self.assertIs(run.call_args.kwargs['stderr'],err)
        self.assertEqual(run.call_args.args[1],['diagnostic','literal argument'])
        self.assertEqual(out.getvalue(),'')
        summary.assert_called_once()
        self.assertEqual(json.loads(summary.call_args.args[0])['enrollment_id'],'a'*32)

    def test_default_cli_keeps_sanitized_json_and_no_stream_sinks(self):
        receipt={'enrollment_id':'b'*32,'command':{'exit_code':0},
                 'capture':{'stored_bytes':9,'complete':True},'cleanup':{'status':'retained'}}
        out=io.StringIO()
        with contextlib.redirect_stdout(out),mock.patch.object(autopilot_tasks,'run',return_value=(receipt,0)) as run:
            code=cli.main(['autopilot','run','--state-dir','/physical/state','--','diagnostic'])
        self.assertEqual(code,0)
        self.assertIsNone(run.call_args.kwargs.get('stdout'))
        self.assertIsNone(run.call_args.kwargs.get('stderr'))
        self.assertEqual(json.loads(out.getvalue())['enrollment_id'],'b'*32)
