#!/usr/bin/env python3
"""Add advisory APS SessionStart guidance, preserving unrelated native settings.

Existing configuration is backed up privately before an atomic replacement.
This never changes permissions, enables disabled hooks, or bypasses Codex trust.
Configuration read-back is NOT proof a native runtime has executed the hook.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import uuid

PROVIDERS = {'codex': ('.codex', 'hooks.json'), 'claude': ('.claude', 'settings.json'),
             'gemini': ('.gemini', 'settings.json')}
MODULE = 'agent_pooper_scooper.agent_hook'
MAX_CONFIG = 512 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate settings keys')
        result[key] = value
    return result


def _parse(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                      parse_constant=lambda _x: (_ for _ in ()).throw(ValueError('invalid number')))


def merged_settings(value, provider, python):
    if provider not in PROVIDERS or type(value) is not dict:
        raise ValueError('supported provider and settings object required')
    if not os.path.isabs(python) or '\n' in python or '\0' in python:
        raise ValueError('absolute interpreter required')
    value = copy.deepcopy(value)
    hooks = value.setdefault('hooks', {})
    if type(hooks) is not dict:
        raise ValueError('invalid hooks configuration')
    sessions = hooks.setdefault('SessionStart', [])
    if type(sessions) is not list:
        raise ValueError('invalid session hooks')
    sources = 'startup|resume|clear' if provider == 'gemini' else 'startup|resume|clear|compact|fork'
    definition = {'matcher': '^(' + sources + ')$', 'hooks': [{
        'type': 'command', 'command': shlex.join([python, '-I', '-B', '-m', MODULE, '--provider', provider]),
        'timeout': 5000 if provider == 'gemini' else 5}]}
    if provider == 'gemini':
        definition.pop('matcher')  # Lifecycle matching is exact, not regex.
    if definition in sessions:
        return value
    for group in sessions:
        if type(group) is not dict or type(group.get('hooks')) is not list:
            raise ValueError('invalid existing session hook')
        for handler in group['hooks']:
            if type(handler) is not dict:
                raise ValueError('invalid existing hook handler')
            if MODULE in str(handler.get('command', '')):
                raise ValueError('different APS hook already configured; review before replacement')
    sessions.append(definition)
    return value


def _open_directory(path):
    path = Path(path)
    if not path.is_absolute() or os.fspath(path) != os.path.normpath(path):
        raise ValueError('absolute normalized physical directory required')
    fd = os.open('/', DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd); fd = nxt
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('owner-controlled directory required')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(parent, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    except FileNotFoundError:
        return None, None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size > MAX_CONFIG):
            raise ValueError('bounded owner-controlled regular settings required')
        raw = stream.read(MAX_CONFIG + 1)
        if len(raw) > MAX_CONFIG:
            raise ValueError('settings limit exceeded')
    return raw, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _new_file(parent, name, raw):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=parent)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.fsync(parent)


def _backups(home):
    parent = _open_directory(home)
    try:
        for name in ('.local', 'share', 'agent-pooper-scooper', 'hook-backups'):
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
            nxt = os.open(name, DIR_FLAGS, dir_fd=parent)
            info = os.fstat(nxt)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                os.close(nxt)
                raise ValueError('unsafe private backup parent')
            os.close(parent); parent = nxt
        if stat.S_IMODE(os.fstat(parent).st_mode) != 0o700:
            raise ValueError('private backup directory required')
        return parent
    except BaseException:
        os.close(parent)
        raise


def install(home, provider, python, dry_run=False):
    if provider not in PROVIDERS or type(dry_run) is not bool:
        raise ValueError('supported provider required')
    home = Path(home)
    # Keep the venv path intact: resolving a Python symlink could lose its venv.
    python = os.path.abspath(python)
    if not os.path.isfile(python) or not os.access(python, os.X_OK):
        raise ValueError('existing executable Python required')
    directory, filename = PROVIDERS[provider]
    parent = _open_directory(home / directory)
    backup = lock = None
    try:
        before, identity = _read(parent, filename)
        value = {} if before is None else _parse(before)
        proposed = merged_settings(value, provider, python)
        result = {'provider': provider, 'status': 'planned' if dry_run else 'configured',
                  'runtime_verified': False, 'trust_review_required': provider == 'codex',
                  'backup_name': None}
        if proposed == value:
            result['status'] = 'unchanged'
            return result
        after = (json.dumps(proposed, indent=2, ensure_ascii=True, allow_nan=False) + '\n').encode()
        if len(after) > MAX_CONFIG:
            raise ValueError('settings limit exceeded')
        if dry_run:
            return result
        backup = _backups(home)
        lock = os.open(provider + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=backup)
        li = os.fstat(lock)
        if (not stat.S_ISREG(li.st_mode) or li.st_nlink != 1 or li.st_uid != os.getuid()
                or stat.S_IMODE(li.st_mode) != 0o600):
            raise ValueError('unsafe installer lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if _read(parent, filename) != (before, identity):
            raise ValueError('settings changed concurrently; nothing replaced')
        if before is not None:
            name = provider + '-' + uuid.uuid4().hex + '.json'
            _new_file(backup, name, before)
            result['backup_name'] = name
        stage = '.aps-hooks-' + uuid.uuid4().hex + '.json'
        _new_file(parent, stage, after)
        if _read(parent, filename) != (before, identity):
            raise ValueError('settings changed concurrently; staged evidence retained')
        if before is None:
            os.link(stage, filename, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(stage, dir_fd=parent)
        else:
            os.replace(stage, filename, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
        current, _ = _read(parent, filename)
        if current != after:
            raise ValueError('configuration read-back mismatch')
        result['configuration_sha256'] = hashlib.sha256(current).hexdigest()
        return result
    finally:
        for fd in (lock, backup, parent):
            if fd is not None:
                os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--provider', choices=tuple(PROVIDERS), required=True)
    parser.add_argument('--python', required=True)
    parser.add_argument('--home', default=os.fspath(Path.home()))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = install(args.home, args.provider, args.python, args.dry_run)
    except Exception:
        print('APS hook configuration refused; inspect settings and private backups. No permission or trust override was applied.', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
