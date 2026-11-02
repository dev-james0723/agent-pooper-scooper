#!/usr/bin/env python3
"""Install only the dedicated autopilot LaunchAgent for a verified local CLI.

No upgrades, wheel downloads, source adoption, unrelated services or state
removal. The caller initializes policy explicitly. --no-start writes only the
new plist; starting a real service remains a separate owner-controlled effect.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys

from agent_pooper_scooper import __version__, autopilot, disk

LABEL = 'io.agent-pooper-scooper.autopilot'


def _absolute(path):
    value = os.fspath(path)
    if not os.path.isabs(value) or os.path.normpath(value) != value:
        raise ValueError('physical absolute path required')
    return value


def install(aps, state_dir, launch_agents, *, no_start=True, platform=None):
    aps, state_dir, launch_agents = map(_absolute, (aps, state_dir, launch_agents))
    if type(no_start) is not bool:
        raise ValueError('explicit start mode required')
    if not no_start and (platform or sys.platform) != 'darwin':
        raise ValueError('launchd requires macOS')
    autopilot.status(state_dir)  # Never initialize/adopt implicitly.
    parent, leaf = disk._open_parent(aps)
    exe = None
    try:
        exe = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        info = os.fstat(exe)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022 or not info.st_mode & 0o100 or info.st_size > 1024 * 1024):
            raise ValueError('owned installed executable required')
        digest = hashlib.sha256(os.read(exe, 1024 * 1024 + 1)).hexdigest()
        result = subprocess.run([aps, '--version'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=5, check=False, text=True)
        if result.returncode != 0 or result.stdout.strip() != 'aps ' + __version__:
            raise ValueError('installed release version mismatch')
        now = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (now.st_dev, now.st_ino, now.st_mtime_ns, now.st_size) != (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size):
            raise ValueError('executable changed')
    finally:
        if exe is not None:
            os.close(exe)
        os.close(parent)
    parent, leaf = disk._open_parent(launch_agents)
    directory = None
    try:
        directory = os.open(leaf, disk._DIR_FLAGS, dir_fd=parent)
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('owned LaunchAgents directory required')
        configuration = {'Label': LABEL, 'ProgramArguments': [aps, 'autopilot', 'tick', '--state-dir', state_dir],
                         'StartInterval': 60, 'RunAtLoad': True, 'Nice': 10, 'ProcessType': 'Background',
                         'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null',
                         'APSVersion': __version__, 'APSExecutableSHA256': digest}
        raw = plistlib.dumps(configuration, sort_keys=True)
        name = LABEL + '.plist'
        exists = False
        try:
            target = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        except FileNotFoundError:
            target = None
        if target is not None:
            with os.fdopen(target, 'rb') as stream:
                autopilot._private(os.fstat(stream.fileno()))
                if stream.read(65537) != raw:
                    raise ValueError('unknown service destination refused')
            exists = True
        else:
            target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
            with os.fdopen(target, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory)
        if not no_start:
            # No kickstart/replay after an uncertain bootstrap. Caller inspects
            # launchctl print and may explicitly invoke this installer again.
            result = subprocess.run(['/bin/launchctl', 'bootstrap', 'gui/' + str(os.getuid()), launch_agents + '/' + name],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=10, check=False)
            if result.returncode != 0:
                raise ValueError('service start refused or uncertain; inspect launchctl')
        return {'status': 'already_installed' if exists else 'installed', 'label': LABEL,
                'interval_seconds': 60, 'version': __version__, 'start_requested': not no_start}
    finally:
        if directory is not None:
            os.close(directory)
        os.close(parent)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--aps', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--launch-agents', default=str(Path.home() / 'Library/LaunchAgents'))
    parser.add_argument('--no-start', action='store_true')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(args.aps, args.state_dir, args.launch_agents, no_start=args.no_start)))
        return 0
    except Exception:
        print(json.dumps({'error': 'Autopilot installation refused or uncertain; inspect state and launchd before retrying.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
