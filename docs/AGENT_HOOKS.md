# Advisory native-agent startup integration

APS can add bounded SessionStart context for Codex, Claude Code and Gemini CLI.
It reminds new/resumed sessions to use the APS runner for suitable **new,
reproducible disposable diagnostic output**. It never grants tool permission,
rewrites commands, reads transcripts, cleans native files, or enrolls a task
merely because an agent process exists. Guidance is not deterministic capture:
only an actual APS enrollment receipt establishes managed cleanup coverage.

## Installation

Use the Python executable from the verified APS release, not an unrelated system
Python. Install only providers already configured on the workstation:

```sh
python3 tools/install_agent_hooks.py --provider claude --python /physical/aps-release/venv/bin/python --dry-run
python3 tools/install_agent_hooks.py --provider claude --python /physical/aps-release/venv/bin/python
# Other supported providers: codex, gemini.
```

The installer preserves unrelated settings and hooks and writes an owner-private
backup before replacing an existing JSON configuration. It refuses symlinks,
hard-linked or writable-by-others settings, malformed or oversized JSON,
duplicate keys, concurrent changes, and a conflicting earlier APS hook. A
matching install is idempotent. It does not create missing provider directories,
modify approval/sandbox modes, enable hooks disabled by policy, or trust hooks.

Codex requires review/trust of the exact hook definition in its native `/hooks`
interface before a new or changed non-managed hook runs. This installer reports
that requirement; it never uses a trust-bypass flag or silently edits trust
state. A configured hook is not reported as runtime verified. Native runtime
behavior and current-session reload must be checked separately.

Hook input is bounded to 16 KiB and one second. Only the event/source enum is
used; prompts, command arguments, session identifiers and transcript paths are
never emitted or persisted. Invalid, oversized or incomplete input yields an
empty JSON object without blocking startup. No network or filesystem mutation
is performed by the hook. The local executable is invoked with isolated Python
mode to avoid inherited module paths and user startup code.

## Disable / rollback

Remove only the exact APS SessionStart handler from the provider's native hooks
configuration (or disable it through the provider's UI). Preserve all other
handlers. Original JSON bytes are kept under the private APS `hook-backups`
directory. Restore a full backup only after comparing subsequent settings
changes; blindly restoring an old file could discard newer user settings.
Disabling startup guidance does not delete APS state or stop the independently
configured autopilot service. Use `aps autopilot disable` to stop automatic
managed collection.

## Verified protocol references

- https://developers.openai.com/codex/hooks
- https://code.claude.com/docs/en/hooks
- https://geminicli.com/docs/hooks/reference/

The protocols provide advisory SessionStart context. APS does not assume that
configuration alone proves a model followed the guidance or that all native
agent-created artifacts became managed.
