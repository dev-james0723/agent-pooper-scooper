#!/usr/bin/env python3
"""Installed CLI acceptance on NEW synthetic fixtures; leaves all evidence.

Run using the installed release's Python from outside the source tree, with
PYTHONPATH unset. No monitor/GUI/service or existing personal data is changed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def accept(aps, fixture_parent):
    if not os.path.isabs(aps) or not os.path.isabs(fixture_parent):
        raise ValueError('absolute installed CLI and physical fixture parent required')
    base = Path(tempfile.mkdtemp(prefix='aps-accept-', dir=fixture_parent))
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    def call(*args, expected=0):
        result = subprocess.run([aps, 'autopilot', *args], cwd=str(base), env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=30, check=False, text=True)
        if result.returncode != expected:
            raise AssertionError('installed CLI acceptance refused; inspect retained fixture')
        return json.loads(result.stdout)
    state = str(base / 'fixture')
    production = str(base / 'production-default')
    assert call('init', '--state-dir', production)['retention_seconds'] == 86400
    assert call('init', '--state-dir', state, '--retention-seconds', '1')['enabled'] is False
    call('enable', '--state-dir', state)
    def run(source, disposable=True, expected=0):
        argv = ['run', '--state-dir', state]
        if disposable:
            argv.append('--disposable-output')
        return call(*argv, '--', sys.executable, '-c', source, expected=expected)
    success = run("print('synthetic disposable')")
    held = run("print('synthetic held')")
    failed = run("print('synthetic failure'); exit(7)", expected=7)
    primary = run("print('synthetic primary')", disposable=False)
    call('hold', '--state-dir', state, '--enrollment-id', held['enrollment_id'])
    call('disable', '--state-dir', state)
    time.sleep(2.1)
    assert call('tick', '--state-dir', state)['new_removed']['count'] == 0
    call('enable', '--state-dir', state)
    first = call('tick', '--state-dir', state)
    assert first['new_removed'] == {'count': 1, 'bytes': len(b'synthetic disposable\n')}
    assert first['reclaimed_bytes'] is None
    second = call('tick', '--state-dir', state)
    assert second['new_removed'] == {'count': 0, 'bytes': 0}
    assert second['removed'] == first['removed']
    def blobs(receipt):
        return list((base / 'fixture/runs' / receipt['enrollment_id'] / 'store/artifacts').glob('*.blob'))
    assert not blobs(success)
    for receipt in (held, failed, primary):
        assert blobs(receipt)
    # Verify native APFS observation through the installed package, without a
    # fixture mount override. Other platforms may exercise supported native FS.
    from agent_pooper_scooper import __version__, executor_platform
    path = base / 'fixture/runs' / held['enrollment_id'] / 'store/artifacts'
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        native = executor_platform.observe(fd, str(path))
    finally:
        os.close(fd)
    return {'status': 'passed', 'version': __version__, 'confirmed_removed': first['removed'],
            'repeat_new_removed': second['new_removed'], 'held_failed_primary_preserved': True,
            'native_backend': native['backend'], 'reclaimed_bytes': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--aps', required=True)
    parser.add_argument('--fixture-parent', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(accept(args.aps, args.fixture_parent)))
        return 0
    except Exception:
        print(json.dumps({'status': 'refused_or_failed', 'evidence': 'new fixture retained for inspection'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
