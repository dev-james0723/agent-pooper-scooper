"""Advisory SessionStart context for native coding agents; no cleanup or grants.

Only event/source enums are consumed. Transcripts, prompts, arguments, paths and
session identifiers are neither read from disk nor emitted or persisted.
"""
from __future__ import annotations

import argparse
import json
import os
import selectors
import sys
import time

from . import schema

MAX_INPUT = 16384
CONTEXT = '''APS safe cleanup is available on this workstation. Native process discovery is observational: existing sessions, worktrees, source, credentials and caches are not automatically enrolled.
For NEW reproducible, disposable diagnostic stdout/stderr only, check owner policy once with:
"$HOME/.local/bin/aps" autopilot status --state-dir "$HOME/.local/share/agent-pooper-scooper/autopilot"
When enabled and the diagnostic fits a noninteractive 16 MiB/3600-second batch, use:
"$HOME/.local/bin/aps" autopilot run --state-dir "$HOME/.local/share/agent-pooper-scooper/autopilot" --stream-output --disposable-output -- COMMAND ARG...
Use literal argv, not a new shell wrapper. Do not wrap an entire agent, interactive command, primary deliverable, backup, export, model download or important output. Child-created source/build files are not captured or cleaned. APS retains failed/incomplete runs and supports hold/disable; successful disposable captured output becomes eligible after its recorded retention. Never relabel old or unknown native files as disposable. Respect all existing command approvals and sandbox boundaries. If enrollment fails, inspect whether the command started before any retry; never rerun an ambiguous command automatically. Startup guidance is not proof that a task is enrolled or any bytes were removed. Report only confirmed receipts; reclaimed SSD capacity remains unknown.'''


def response(value, provider):
    if (provider not in ('codex', 'claude', 'gemini') or type(value) is not dict
            or value.get('hook_event_name') != 'SessionStart'
            or value.get('source') not in ('startup', 'resume', 'clear', 'compact', 'fork')):
        return {}
    output = {'additionalContext': CONTEXT}
    if provider != 'gemini':
        output['hookEventName'] = 'SessionStart'
    return {'hookSpecificOutput': output}


def _input():
    raw = bytearray()
    descriptor = sys.stdin.fileno()
    was_blocking = os.get_blocking(descriptor)
    try:
        os.set_blocking(descriptor, False)
        with selectors.DefaultSelector() as watcher:
            watcher.register(descriptor, selectors.EVENT_READ)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                for _key, _mask in watcher.select(0.05):
                    chunk = os.read(descriptor, MAX_INPUT + 1 - len(raw))
                    if not chunk:
                        return schema.parse_json(raw.decode('utf-8'))
                    raw.extend(chunk)
                    if len(raw) > MAX_INPUT:
                        return None
    finally:
        os.set_blocking(descriptor, was_blocking)
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--provider', required=True, choices=('codex', 'claude', 'gemini'))
    args = parser.parse_args(argv)
    try:
        result = response(_input(), args.provider)
    except Exception:
        result = {}  # Never block startup or print untrusted input on failure.
    sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
