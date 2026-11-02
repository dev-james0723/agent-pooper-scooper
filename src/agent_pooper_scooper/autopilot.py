"""Owner-enabled policy for NEW APS-created stores, never native agent data.

The policy lock serializes hold/disable and the entire collection transaction.
A persisted collecting intent is permanent until operator inspection; no retry
or fresh grant follows an ambiguous outcome. JSON is private control state.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import re
import stat
import threading
import time
import uuid

from . import disk, schema

MAX_STATE = 256 * 1024
MAX_RUNS = 128
ID = re.compile(r'[0-9a-f]{32}\Z')
MARKER = 'agent-pooper-scooper/autopilot'
_MUTEX = threading.Lock()


class AutopilotError(ValueError):
    """Content-free policy refusal."""


def _physical_path(value):
    path = os.fspath(value)
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise AutopilotError('physical absolute state required')
    return path


def _integer(value, minimum=0, maximum=2**53):
    return type(value) is int and minimum <= value <= maximum


def _identity(info):
    return [info.st_dev, info.st_ino]


def _private(info, directory=False):
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise AutopilotError('private physical state required')


def _read(fd, name):
    out = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    with os.fdopen(out, 'rb') as stream:
        _private(os.fstat(stream.fileno()))
        raw = stream.read(MAX_STATE + 1)
    if len(raw) > MAX_STATE:
        raise AutopilotError('state limit')
    return schema.parse_json(raw.decode('utf-8'))


def _write(fd, name, value, replace=False):
    raw = (json.dumps(value, allow_nan=False, ensure_ascii=True) + '\n').encode()
    if len(raw) > MAX_STATE:
        raise AutopilotError('state limit')
    stage = 'stage-' + uuid.uuid4().hex if replace else name
    out = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
    with os.fdopen(out, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        _private(os.stat(name, dir_fd=fd, follow_symlinks=False))
        os.replace(stage, name, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)


def _validate(value):
    if (type(value) is not dict or set(value) != {'schema', 'schema_version', 'enabled', 'retention_seconds', 'runs', 'last_tick', 'cursor'}
            or value['schema'] != MARKER or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['enabled']) is not bool or not _integer(value['retention_seconds'], 1, 31536000)
            or not _integer(value['cursor'], 0, MAX_RUNS - 1)
            or type(value['runs']) is not list or len(value['runs']) > MAX_RUNS
            or (value['last_tick'] is not None and not _integer(value['last_tick']))):
        raise AutopilotError('invalid policy')
    seen = set()
    for row in value['runs']:
        if (type(row) is not dict or set(row) != {'id', 'status', 'disposable', 'hold', 'retention', 'finished_at',
                'generation', 'store_identity', 'objects', 'captured_bytes', 'removed_count', 'removed_bytes'}
                or not isinstance(row['id'], str) or not ID.fullmatch(row['id']) or row['id'] in seen
                or row['status'] not in ('pending', 'success', 'retained', 'collecting', 'collected', 'blocked')
                or type(row['disposable']) is not bool or type(row['hold']) is not bool
                or not _integer(row['retention'], 1, 31536000)
                or any(not _integer(row[key]) for key in ('captured_bytes', 'removed_count', 'removed_bytes'))
                or row['captured_bytes'] > 16 * 1024 * 1024
                or row['removed_count'] > 64 or row['removed_bytes'] > row['captured_bytes']
                or (row['finished_at'] is not None and not _integer(row['finished_at']))
                or (row['generation'] is not None and not _integer(row['generation'], 1))
                or (row['store_identity'] is not None and (type(row['store_identity']) is not list
                    or len(row['store_identity']) != 2 or any(not _integer(n) for n in row['store_identity'])))
                or type(row['objects']) is not list or len(row['objects']) > 64
                or any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', n) for n in row['objects'])
                or len(set(row['objects'])) != len(row['objects'])):
            raise AutopilotError('invalid enrollment')
        if (row['status'] in ('success', 'collecting', 'collected')
                and (not row['disposable'] or row['finished_at'] is None
                     or row['generation'] is None or row['store_identity'] is None)):
            raise AutopilotError('incomplete successful enrollment')
        seen.add(row['id'])
    return value


def _save(fd, value):
    _validate(value)
    _write(fd, 'state.json', value, replace=True)


@contextmanager
def _state(state_dir):
    if not _MUTEX.acquire(timeout=0.5):
        raise AutopilotError('policy busy')
    parent = root = lock = runs = None
    try:
        path = _physical_path(state_dir)
        parent, leaf = disk._open_parent(path)
        root = os.open(leaf, disk._DIR_FLAGS, dir_fd=parent)
        _private(os.fstat(root), True)
        lock = os.open('lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root)
        _private(os.fstat(lock))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AutopilotError('policy busy') from None
        runs = os.open('runs', disk._DIR_FLAGS, dir_fd=root)
        _private(os.fstat(runs), True)
        marker = _read(root, 'marker.json')
        if type(marker) is not dict or type(marker.get('schema_version')) is not int or marker != {'schema': MARKER, 'schema_version': 1, 'root': _identity(os.fstat(root)),
                      'runs': _identity(os.fstat(runs)), 'lock': _identity(os.fstat(lock))}:
            raise AutopilotError('state identity changed')
        # A failed atomic write is evidence requiring inspection, not permission
        # to discard it. Unknown state entries block further mutations too.
        names = []
        with os.scandir(root) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > 4:
                    raise AutopilotError('unknown policy entries')
        if set(names) != {'marker.json', 'state.json', 'lock', 'runs'}:
            raise AutopilotError('incomplete state')
        yield root, _validate(_read(root, 'state.json'))
    finally:
        for fd in (runs, lock, root, parent):
            if fd is not None:
                os.close(fd)
        _MUTEX.release()


def initialize(state_dir, retention_seconds=86400, enabled=False):
    if not _integer(retention_seconds, 1, 31536000) or type(enabled) is not bool:
        raise AutopilotError('invalid policy')
    from .runner import _new_run
    path = _physical_path(state_dir)
    with _new_run(path) as fd:
        os.mkdir('runs', 0o700, dir_fd=fd)
        runs = os.open('runs', disk._DIR_FLAGS, dir_fd=fd)
        lock = os.open('lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            os.fsync(lock)
            _write(fd, 'marker.json', {'schema': MARKER, 'schema_version': 1, 'root': _identity(os.fstat(fd)),
                                     'runs': _identity(os.fstat(runs)), 'lock': _identity(os.fstat(lock))})
            _write(fd, 'state.json', {'schema': MARKER, 'schema_version': 1, 'enabled': enabled,
                                    'retention_seconds': retention_seconds, 'runs': [], 'last_tick': None, 'cursor': 0})
        finally:
            os.close(runs)
            os.close(lock)
    return status(state_dir)


def set_enabled(state_dir, enabled):
    if type(enabled) is not bool:
        raise AutopilotError('explicit policy required')
    with _state(state_dir) as (fd, value):
        value['enabled'] = enabled
        _save(fd, value)
    return status(state_dir)


def _public(value):
    return {'schema': MARKER + '-status', 'schema_version': 1, 'enabled': value['enabled'],
            'retention_seconds': value['retention_seconds'], 'enrolled_runs': len(value['runs']),
            'coverage': 'new APS wrapper capture only; discovered native agents are not enrolled',
            'runs': [{'id': r['id'], 'status': r['status'], 'hold': r['hold']} for r in value['runs']],
            'removed': {'count': sum(r['removed_count'] for r in value['runs']),
                        'bytes': sum(r['removed_bytes'] for r in value['runs'])},
            'retained': {'known_bytes': sum(r['captured_bytes'] - r['removed_bytes'] for r in value['runs']
                                          if r['status'] not in ('pending', 'collecting', 'blocked')),
                         'complete': all(r['status'] not in ('pending', 'collecting', 'blocked') for r in value['runs'])},
            'candidate': None, 'reclaimed_bytes': None, 'last_tick': value['last_tick']}


def status(state_dir):
    with _state(state_dir) as (_, value):
        return _public(value)


def set_hold(state_dir, enrollment_id, hold=True):
    if type(hold) is not bool or not isinstance(enrollment_id, str) or not ID.fullmatch(enrollment_id):
        raise AutopilotError('invalid hold')
    with _state(state_dir) as (fd, data):
        row = next((r for r in data['runs'] if r['id'] == enrollment_id), None)
        if row is None:
            raise AutopilotError('unknown enrollment')
        row['hold'] = hold
        _save(fd, data)
    return status(state_dir)


def tick(state_dir):
    """One bounded pass. Never reconcile, retry or regrant ambiguous effects."""
    from . import agent_observer, executor, runner
    from .authority import ManagedAuthority
    from .managed_cli import _capability
    started = time.monotonic()
    discovery = agent_observer.observe_agents()
    new = {'count': 0, 'bytes': 0}
    candidate = {'count': 0, 'bytes': 0}
    skipped = {}
    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1
    with _state(state_dir) as (fd, data):
        processed = 0
        total = len(data['runs'])
        order = [(data['cursor'] + n) % total for n in range(total)] if total else []
        for index in order:
            row = data['runs'][index]
            if not data['enabled']:
                skip('disabled')
                break
            if row['hold']:
                skip('held')
                continue
            if row['status'] != 'success' or not row['disposable']:
                skip(row['status'])
                continue
            now = int(time.time())
            if row['finished_at'] is None or not 0 <= now - row['finished_at'] >= row['retention']:
                skip('retention_or_clock')
                continue
            if processed >= 16 or time.monotonic() - started >= 25:
                skip('tick_budget')
                break
            processed += 1
            data['cursor'] = (index + 1) % total
            path = os.fspath(state_dir) + '/runs/' + row['id']
            try:
                if (row['generation'] is None or row['store_identity'] is None
                        or not row['objects']):
                    skip('no_confirmed_objects')
                    continue
                # Both the private owner file and authority validate physical
                # paths and identities again; no caller supplies a store path.
                if _identity(os.stat(path + '/store', follow_symlinks=False)) != row['store_identity']:
                    raise AutopilotError('store changed')
                owner = ManagedAuthority(path + '/store', owner_key=_capability(path + '/owner.json', 'owner'))
                snap = owner.snapshot(runner._TASK)
                if (snap['task_status'] != 'completed' or snap['producer_generation'] != row['generation']
                        or snap['recovery_required'] or snap['pending_operations']):
                    skip('lifecycle_or_recovery')
                    continue
                plan = executor.create_plan(owner, runner._TASK, artifact_ids=row['objects'], deadline_seconds=2)
                if (set(plan['candidate']['artifact_ids']) != set(row['objects'])
                        or plan['candidate']['apparent_bytes'] != row['captured_bytes']):
                    skip('executor_protection')
                    continue
                candidate['count'] += plan['candidate']['count']
                candidate['bytes'] += plan['candidate']['apparent_bytes']
                # The exact permanent operation ID derives only from this
                # create-only enrollment. Persist intent BEFORE even approval.
                row['status'] = 'collecting'
                _save(fd, data)
                operation = 'autopilot-' + row['id']
                grant = executor.approve_plan(owner, plan, operation, ttl_seconds=10, deadline_seconds=2)
                result = executor.collect(owner, plan, execute=True, approval_id=grant['approval_id'],
                                          operation_id=operation, deadline_seconds=3, public=True)
                removed = result['removed']
                if (type(removed) is not dict or set(removed) != {'count', 'bytes'}
                        or not _integer(removed['count'], 0, len(row['objects']))
                        or not _integer(removed['bytes'], 0, row['captured_bytes'])):
                    raise AutopilotError('unverified receipt')
                row['removed_count'] = removed['count']
                row['removed_bytes'] = removed['bytes']
                row['status'] = ('collected' if result['status'] == 'COMPLETED'
                                 and result.get('reconciliation_required') is False else 'blocked')
                _save(fd, data)
                new['count'] += removed['count']
                new['bytes'] += removed['bytes']
            except Exception:
                # Even a safe pre-effect refusal is conservatively inspect-only.
                # A failed durable finish remains collecting on disk if saving
                # fails; neither state is automatically eligible again.
                row['status'] = 'blocked'
                _save(fd, data)
                skip('refused_or_uncertain')
        data['last_tick'] = int(time.time())
        _save(fd, data)
        result = _public(data)
        result.update(new_removed=new, candidate=candidate, skipped=skipped, discovery=discovery)
        return result
