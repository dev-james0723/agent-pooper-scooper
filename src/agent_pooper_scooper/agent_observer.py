"""Bounded, ephemeral same-UID executable observations. Never cleanup authority."""
from __future__ import annotations

import os
import selectors
import subprocess
import time

MAX_OUTPUT = 1024 * 1024
MAX_ROWS = 16384
PROVIDERS = frozenset(('codex', 'claude', 'gemini', 'aider', 'opencode'))


def _process_output():
    # comm deliberately excludes arguments/environment. A generic node/python
    # host cannot establish a script's identity without inspecting private argv.
    process = subprocess.Popen(['/bin/ps', '-axo', 'uid=,pid=,comm='],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, close_fds=True)
    raw = bytearray()
    try:
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 2
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise TimeoutError
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        raw.extend(chunk)
                        if len(raw) > MAX_OUTPUT:
                            raise ValueError
            if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                raise ValueError
        return bytes(raw)
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()  # Only the observer's own ps child, never an agent.
        process.wait(timeout=1)


def observe_agents():
    value = {'status': 'unknown', 'providers': [], 'cleanup_authority': 'none',
             'coverage': 'same-UID known executable names only; script hosts and native capture unenrolled'}
    try:
        raw = _process_output()
        if not isinstance(raw, bytes) or len(raw) > MAX_OUTPUT:
            raise ValueError
        rows = raw.decode('utf-8').splitlines()
        if not rows or len(rows) > MAX_ROWS:
            raise ValueError
        counts = {}
        seen = set()
        for row in rows:
            fields = row.strip().split(None, 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit() or int(fields[1]) <= 0:
                raise ValueError
            uid, pid, command = fields
            if pid in seen:
                raise ValueError
            seen.add(pid)
            name = command.rsplit('/', 1)[-1]
            if int(uid) == os.getuid() and name in PROVIDERS:
                counts[name] = counts.get(name, 0) + 1
        value.update(status='observed', providers=[{'provider': name, 'count': count,
                     'confidence': 'executable-name'} for name, count in sorted(counts.items())])
    except Exception:
        pass
    return value
