# Install the macOS preview from a verified wheel

The first-install helper uses an existing local release wheel and its expected
SHA-256 digest. It does not download packages, replace an existing installation,
edit shell startup files, or clean existing native files. Python 3.10+ with
`venv` and bundled `ensurepip` is required.

Run the installer from the reviewed source checkout:

```sh
python3 tools/install_macos.py --wheel /physical/release/agent_pooper_scooper-0.1.0a1-py3-none-any.whl \
  --sha256 VERIFIED_RELEASE_SHA256 --dry-run
python3 tools/install_macos.py --wheel /physical/release/agent_pooper_scooper-0.1.0a1-py3-none-any.whl \
  --sha256 VERIFIED_RELEASE_SHA256
```

Replace the example filename/version and digest with the actual reviewed
release. A digest authenticates equality with your expected release bytes;
an arbitrary digest supplied alongside an untrusted wheel is not a code review.

The dry run reads and validates the wheel and all destination paths. It creates
no files and runs no subprocesses. The installer checks the expected package
name/version, pure-Python wheel tag, console entry point, bounded archive
members and matching embedded metadata. Installation uses a private verified
copy of those bytes and `pip --isolated --no-index --no-deps`, then checks
dependencies and the installed CLI version.

## Installed entry points

- `~/.local/bin/aps` links to the versioned virtual environment.
- `~/Desktop/Agent-Pooper-Scooper.command` runs the bounded monitor tick and opens
  its Markdown report, including current pressure and inventory status/age.
- `~/Library/LaunchAgents/io.agent-pooper-scooper.monitor.plist` registers a
  low-priority pressure/report monitor every 15 minutes and at login.
- `~/.local/share/agent-pooper-scooper/releases/<version>/venv` contains the
  isolated package installation and the adjacent verified wheel.
- The private installation receipt is
  `~/.local/share/agent-pooper-scooper/installation.json`.

Use the explicit CLI path if your shell does not already include `~/.local/bin`:

```sh
~/.local/bin/aps --version
~/.local/bin/aps host scan --format markdown
~/.local/bin/aps monitor status --state-dir ~/.local/share/agent-pooper-scooper/monitor
```

The monitor receives only the installed executable and its private state
directory. It receives no producer/owner credentials. Its command is
`aps monitor tick --state-dir ... --public --notify`. Standard output is
discarded; errors go to a mode-0600 file under the installation prefix.
LaunchAgent registration is checked through `launchctl print`; registration
does not itself prove a completed report or a visible notification. Inspect
the monitor status for actual run evidence. Native disk reporting remains
read-only, and managed collection still requires its separate explicit scope.

### Slow or unavailable inventory locations

Each tick saves its fresh capacity report before starting a detailed inventory.
If a low-space notification is due, its durable intent and provider result are
also recorded before the inventory begins. The saved capacity result therefore
remains available when a native directory is slow or blocked.

Inventory runs in a separate read-only process with a **30-second supervision
deadline**, a four-MiB output cap, and the existing entry/depth limits. Use
`--scan-timeout-seconds` on `monitor tick` to select 1–300 seconds. This deadline
does not bound the entire tick: capacity/state filesystem operations and the
notification provider have their own behavior; a notification has a separate
ten-second timeout. The worker receives no monitor directory descriptor, output
path or producer/owner credentials. Python bytecode writes are disabled.

`scan_status` records the latest attempt separately from `last_scan_epoch`,
which changes only after a completed inventory. A timed-out, invalid or failed
attempt preserves the last successful inventory and its original measurement
timestamp. Failed attempts use the same default six-hour retry interval as
successful scans, while capacity checks continue every 15 minutes. An explicit
change of roots requests a new attempt. `scan_performed` is true only when this
tick obtains a completed inventory; `scan_attempted` also records unsuccessful
attempts.

On a timeout, the parent signals only the child it owns and waits at most one
additional second for that child. A process stuck in kernel I/O may remain
alive; the report then says `termination_pending`. The worker holds an inherited
**read-only inventory lease**, separate from the monitor's writer lock. The
parent closes its own lease descriptor without unlocking it. This prevents a
later tick from launching another inventory while the old worker remains
alive, without blocking fresh capacity reports. The kernel releases the lease
when the worker exits, allowing a later scheduled attempt. Saved worker PIDs
are diagnostic only; a later tick never sends a signal to a saved PID.

The Desktop shortcut uses the bounded monitor command. For a readable saved
report without starting a scan, run:

```sh
~/.local/bin/aps monitor status --state-dir ~/.local/share/agent-pooper-scooper/monitor --format markdown
```

Markdown separates the fresh capacity timestamp from the last completed
inventory's timestamp. `--public` output intentionally omits native inventory
details and retains only the validated monitor summary.

`--no-start` installs the package, shortcut and plist without registering or
starting the service. `--prefix` selects a new directory inside your home for
the versioned installation; the CLI shortcut and LaunchAgent locations stay
fixed.

## Existing data and incomplete installation

All destination preflights happen before installation writes begin. Existing
installation prefixes, environments, shortcuts, CLI links and LaunchAgent
files are refused. Symlink ancestors and unexpected directory owners are
refused. Existing parent directories are never chmodded. Only new generated
directories/files receive private permissions.

This release has no upgrade, automatic rollback or uninstall operation.
If an installation fails partway, its files remain available for inspection.
Do not remove an unfamiliar prefix or overwrite an existing launcher to retry.
Inspect the reported phase, owned installation paths and actual service state
first. A later retry is deliberately refused until the partial installation
has been explicitly reconciled.
