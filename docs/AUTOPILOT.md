# Safe local autopilot (0.2.0a1)

Autopilot automatically collects **new APS wrapper-captured disposable stdout and
stderr** after confirmed successful command completion and retention. Each
periodic tick creates a fresh exact managed plan and grant and calls the existing
fenced executor. It does not recursively sweep a directory or adopt existing
files. Reclaimed physical capacity is always unknown.

Automatic agent discovery observes same-UID exact executable names (`codex`,
`claude`, `gemini`, `aider`, `opencode`) through a bounded `ps comm` query. It
runs automatically on each scheduled tick and returns aggregate counts and name-based confidence, never argv, paths, PIDs,
environment, keys or transcripts. Generic Node/Python script hosts, renamed
binaries and unrecognized agents are outside detection coverage. Process
absence, disappearance and apparent completion never authorize deletion.
**A discovered process is not an enrolled capture.** No native agent executable
shim, session, transcript, worktree, credential, cache or source file is changed.

## Owner controls and agent entry point

Use a new physical absolute directory whose existing parent is owner-controlled.
On macOS, use `/private/tmp`, rather than the `/tmp` symlink, for fixtures.
The examples' `/physical` paths are placeholders, not discovered machine paths.

```sh
aps autopilot init --state-dir /physical/aps/autopilot
aps autopilot enable --state-dir /physical/aps/autopilot
aps autopilot run --state-dir /physical/aps/autopilot --stream-output --disposable-output \
  -- /physical/python -c 'print("temporary diagnostic")'
aps autopilot status --state-dir /physical/aps/autopilot
aps autopilot tick --state-dir /physical/aps/autopilot
aps autopilot hold --state-dir /physical/aps/autopilot --enrollment-id ENROLLMENT_ID
aps autopilot release --state-dir /physical/aps/autopilot --enrollment-id ENROLLMENT_ID
aps autopilot disable --state-dir /physical/aps/autopilot
```

Initialization is create-only and defaults to **disabled, 86400-second (24-hour)
retention**. Enable is the owner's scoped policy authorization. Every run snapshots
its retention and registers that same grace with the broker before capture.
There is no command to shorten an existing enrollment's retention. A final
conservative whole-second boundary can add up to two seconds to retention.

`run` takes literal argv after `--`; it never invokes a shell or interactive stdin.
Only use `--disposable-output` for replaceable diagnostics whose primary result
is stored elsewhere. Without it, output remains protected. Child-created files
stay outside APS coverage regardless of this flag. By default stdout is a sanitized
JSON summary. Add `--stream-output` to explicitly mirror private command stdout/stderr
to the calling agent; the bounded receipt then goes to stderr rather than corrupting
child stdout. Never publish streamed private diagnostics. Captured bytes and the
owner key remain in the private run directory. This is a cooperative protocol,
not a sandbox against raw same-UID writers. See [execution safety](EXECUTION_SAFETY.md).

Failed commands, timeout, output overflow, incomplete pipe capture and uncertain
completion retain output. Active, paused, resumable, changed-generation,
referenced and leased objects retain; **expired unreleased leases also protect**.
Unknown files, altered identities, symlinks, hardlinks, unsupported mounts and
pending authority effects cannot become automatic deletion targets. A run must
have its exact captured object set eligible; partial eligibility retains the set.

Hold and disable serialize with ticks. Once hold/disable successfully returns,
subsequent ticks cannot start collection for that scope. A collection already in
progress completes or records uncertainty before the policy change can acquire
the lock; a busy refusal is not an applied hold/disable. Retry a policy change
only after inspecting current status. An enrollment remains pending if its
wrapper crashes; a collecting/blocked run never receives another automatic
grant or operation ID. Inspect private owner receipts and existing reconciliation
interfaces; there is no automatic repair, replay, re-enrollment or state editing.

## Bounds and accounting

State is owner-only (0700 directories; 0600 single-link regular metadata),
physically addressed, schema-validated and bound to root/runs/lock identities.
Atomic replace and fsync preserve policy intent. Interrupted staging files cause
a refusal and remain available for inspection. Native namespace/hardware limits
from the managed execution contract still apply.

There are at most 128 lifetime enrollments, 16 MiB combined output per run,
64 objects per collection and 16 considered eligible runs per tick. A persisted cursor prevents protected
earlier runs from starving later enrollments. Saturation
refuses enrollment and preserves metadata and tombstones. Each tick stops
starting runs after 25 seconds, with 2-second query/grant and 3-second collection
admission deadlines. Filesystem flushes have no universal wall-clock guarantee.
Busy policy calls fail promptly rather than queueing indefinitely.

Status shows enrollment states, holds, policy, last tick, retained known bytes
and cumulative confirmed removed bytes. Candidate observations and skipped
reasons are reported by the tick; read-only status leaves candidate bytes
unknown. Capturing or uncertain runs make retained totals incomplete. Confirmed
removal counts come only from durable executor receipts. An interrupted policy
save may undercount those receipts; later ticks do not reconstruct or double
count them. No physical reclaimed-byte figure is claimed.

## Dedicated scheduler and rollback

Build a wheel from the reviewed immutable revision and install it offline in a
new versioned environment. Verify its hash and `aps --version` from outside the
source tree. The existing first-install tool (`tools/install_macos.py`) is for a
new installation; it refuses existing destinations. Upgrades and entrypoint
switches remain supervised release actions. Never overwrite an existing release
or monitor/UI service to install this scheduler.

After explicitly initializing policy, invoke the dedicated installer with the
verified installed Python and absolute installed CLI:

```sh
/physical/release/venv/bin/python tools/install_autopilot.py \
  --aps /physical/release/venv/bin/aps \
  --state-dir /physical/aps/autopilot \
  --launch-agents /physical/account/Library/LaunchAgents --no-start
```

Omit `--no-start` only when authorized to bootstrap launchd on that host. The
installer writes only `io.agent-pooper-scooper.autopilot.plist`, with RunAtLoad,
StartInterval=60, Nice=10, absolute literal arguments and no shell. It verifies
the CLI version, executable identity/hash and existing private policy. An exact
reinstall is accepted; unknown or changed destinations are refused. It never
rewrites `io.agent-pooper-scooper.monitor` or the menu-bar app. Repeated bootstrap
can report failure if already loaded; inspect launchd rather than kickstarting
an uncertain earlier effect.

Read back `launchctl print gui/UID/io.agent-pooper-scooper.autopilot` and inspect
`aps autopilot status`. A written plist is not a running-service receipt. To roll
back, disable policy, verify that result, then explicitly boot out only this
service with `launchctl bootout gui/UID/io.agent-pooper-scooper.autopilot`.
Leave its plist, releases, state, owner keys and receipts intact. Never erase
unknown state or old releases as part of rollback.

Native menu status reads the `autopilot` sibling of its configured `menubar`
directory. A custom autopilot directory remains visible through its explicit CLI
but is not automatically registered in the menu. The view separately shows
recognizable processes, captured enrollments and confirmed removals. Existing
exact manual review and confirmation controls are unchanged. Status integration
is not native capture integration.

Copy the dedicated [APS usage skill](../skills/aps-autopilot/SKILL.md) into a new
owner-selected skill directory only during supervised installation. Do not
rewrite other agent executables. Optional advisory startup context is installed
separately through the [native hook integration](AGENT_HOOKS.md), preserving all
unrelated settings and permissions. Guidance is not deterministic enrollment.

## Installed acceptance

From outside source with `PYTHONPATH` unset, run using the new installed Python:

```sh
/physical/release/venv/bin/python /physical/source/tools/accept_autopilot.py \
  --aps /physical/release/venv/bin/aps --fixture-parent /physical/private-fixtures
```

This creates new synthetic states only: production default 86400, fixture
retention 1 second, disposable success, held, failed and protected primary output.
It calls actual installed ticks, verifies native mount observations, exact
confirmed unlink bytes, disable/hold preservation and repeated-tick accounting.
It leaves the fixture and receipts intact, never installs a service or deletes
personal data. On macOS the existing native collector requires writable local
APFS; an unsupported mount or denied broker socket fails the acceptance.
Dual-host deployment, native UI runtime and launchd readback require independent
physical-host verification and are not established by unit tests.

## Research receipt

The implementation reuses the existing APS runner, ManagedAuthority,
ManagedTaskCoordinator and executor instead of introducing a dependency or raw
filesystem cleaner. The pinned containerd and BuildKit coordination references,
licenses and excluded deployment assumptions are recorded in
[execution safety](EXECUTION_SAFETY.md#research-and-limits). Upstream maintenance,
issue responsiveness and security status were not freshly established for this
change; neither project is adopted or executed here. No upstream code or
transitive dependency is copied. Existing local lock, receipt and grace APIs are
the selected compatible pattern; a whole-home heuristic sweeper is rejected.
launchd scheduling follows Apple's [Launch Daemon and Agent guide](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html).
