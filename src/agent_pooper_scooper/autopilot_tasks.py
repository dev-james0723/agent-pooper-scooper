"""Create-only autopilot enrollment around the established managed runner."""
from __future__ import annotations

import os
import time
import uuid

from . import autopilot as policy, runner
from .authority import ManagedAuthority
from .managed_cli import _capability


def run(state_dir, literal_argv, *, disposable_output=False, timeout=runner.DEFAULT_TIMEOUT,
        max_output_bytes=runner.MAX_OUTPUT_BYTES, stdout=None, stderr=None):
    if type(disposable_output) is not bool:
        raise policy.AutopilotError('explicit output classification required')
    ident = uuid.uuid4().hex
    path = os.fspath(state_dir) + '/runs/' + ident
    runner._validate(literal_argv, path, disposable_output, timeout, max_output_bytes)
    # Persist the intent before starting any command or creating its store.
    with policy._state(state_dir) as (fd, data):
        if len(data['runs']) >= policy.MAX_RUNS:
            raise policy.AutopilotError('enrollment full; retain existing metadata')
        row = {'id': ident, 'status': 'pending', 'disposable': disposable_output, 'hold': False,
               'retention': data['retention_seconds'], 'finished_at': None, 'generation': None,
               'store_identity': None, 'objects': [], 'captured_bytes': 0, 'removed_count': 0, 'removed_bytes': 0}
        data['runs'].append(row)
        policy._save(fd, data)
        retention = row['retention']
    # Streaming is explicit: child output may be private. Default callers get
    # sanitized metadata only; explicit sinks retain the runner's bounded I/O.
    receipt, code = runner.run_command(literal_argv, path, clean_success=disposable_output,
                                      defer_cleanup=True, grace_seconds=retention, timeout=timeout,
                                      max_output_bytes=max_output_bytes, stdout=stdout, stderr=stderr)
    with policy._state(state_dir) as (fd, data):
        row = next(r for r in data['runs'] if r['id'] == ident)
        if row['status'] != 'pending':
            raise policy.AutopilotError('enrollment changed')
        row['status'] = 'retained'
        row['captured_bytes'] = receipt['capture']['stored_bytes']
        if (code == 0 and disposable_output and receipt['capture']['complete']
                and receipt['cleanup']['status'] == 'deferred'):
            owner = ManagedAuthority(path + '/store', owner_key=_capability(path + '/owner.json', 'owner'))
            snapshot = owner.snapshot(runner._TASK)
            if snapshot['task_status'] != 'completed':
                raise policy.AutopilotError('unverified completion')
            row.update(status='success', finished_at=int(time.time()) + 1,
                       generation=snapshot['producer_generation'], store_identity=policy._identity(os.stat(path + '/store', follow_symlinks=False)),
                       objects=[o['artifact_id'] for o in receipt['capture']['objects']])
        policy._save(fd, data)
    # Cleanup/capture failure is never hidden behind a successful command exit.
    if receipt['cleanup']['status'] == 'not_completed_inspect_receipts' and code == 0:
        code = 125
    receipt['enrollment_id'] = ident
    return receipt, code
