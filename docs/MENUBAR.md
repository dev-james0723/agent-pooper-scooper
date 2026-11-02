# Native macOS menu-bar preview

The native app is a small SwiftUI status item, not a browser dashboard. Click its
sparkles icon for Overview, Review, History and Settings. It reads actual volume
capacity and the existing monitor's separately timestamped bounded inventory.
No example capacity or invented native-agent activity is displayed.

## Build and install locally

Requires macOS 13+, Apple Swift tools, and an existing Python 3.10+ interpreter.
No third-party Swift dependencies or Python runtime dependencies are downloaded.

```sh
python3 -B -m unittest discover -s tests
(cd macos/APSMenuBar && swift test -j 2)
mkdir -p "$HOME/Applications"
python3 tools/build_menubar.py --output "$HOME/Applications/Agent Pooper Scooper.app"
"$HOME/Applications/Agent Pooper Scooper.app/Contents/Resources/aps-menu" init
open "$HOME/Applications/Agent Pooper Scooper.app"
```

The installer refuses existing destinations and symlink destination ancestors.
An earlier installation is never silently replaced. The app bundles the exact
APS Python sources and records their hash and native build provenance in
`Contents/Resources/build.json`. The final signed executable hash is recorded
in the adjacent `.app.receipt.json` file, after signing. Arbitrary prebuilt
binaries are not accepted by the CLI. The selected local Python interpreter must stay
installed. This is an ad-hoc-signed local preview, **not a notarized public
release**; no signing identity or Gatekeeper exemption is installed.

The app-state parent is the existing private APS installation prefix
`~/.local/share/agent-pooper-scooper`. Install the APS core first, or create this
owner-private parent before initializing the menu state. Only the newly created
`menubar` directory receives configuration and review receipts.

## Managed task enrollment

Configuration is deliberately explicit. In Settings, “Show integration
configuration” reveals the local mode-0600 `config.json`. Its default contains
no enrolled tasks and no peers. Each task must already belong to an APS managed
store; native folders cannot be adopted as scratch.

```json
{
  "schema_version": 1,
  "tasks": [
    {
      "id": "diagnostic-task",
      "label": "Managed diagnostic output",
      "store": "/physical/private/managed-run/store",
      "owner_key_file": "/physical/private/managed-run/owner.json",
      "task_id": "captured-command-output"
    }
  ],
  "peers": []
}
```

The paths above are placeholders, not an instruction to create or enroll native
files. `aps run` creates a compatible managed-output store, but its default
output is **not disposable**. Existing producer policy and retention grace still
apply. The app cannot turn protected or unknown data into disposable data.

A preview has a 60-second confirmation window and names an exact persisted
executor plan. A host switch discards it. Confirmation records a durable intent
before any approval or cleanup. A completed retry returns the historical
receipt and zero newly removed bytes; a pending/partial result refuses replay.
The journal is bounded to 512 entries and refuses additional previews when
full instead of silently deleting audit records.

## Two-Mac inspection

Each Mac runs its own locally installed app. To inspect another Mac in the same
popover, add a peer using an **already configured and authenticated SSH host**:

```json
{"id":"secondary","label":"Other Mac","ssh_alias":"existing-secondary-alias"}
```

The peer must have this app at the same standard per-user Applications path.
SSH is noninteractive, strictly verifies the host key and has bounded connection
and response deadlines. The app neither creates SSH keys nor bypasses trust
checks. Peer inspection only runs the fixed `snapshot` command; remote cleanup
is intentionally unavailable. A missing peer connection is not reported as live
data. No automatic pairing or cross-Mac synchronization is claimed.

## Safety, privacy and performance

Snapshots read only explicitly enrolled accounting, capacity and existing monitor
state. They do not run a recursive host scan. Requests are coalesced; capacity
refreshes at most once per minute automatically. Large/slow/invalid responses
fail visibly. Python module paths and shell startup hooks are not inherited.

The private JSON gateway returns summaries, never owner capabilities, file
contents, command arguments or native file paths. There is no HTTP listener,
cloud telemetry or AO dependency. Native Claude/Codex/AO sessions are **not
automatically enrolled or fenced**. Source code, worktrees, sessions, models,
media, downloads, VMs and shared native caches remain outside menu cleanup.

File-removal receipts do not prove causally reclaimed SSD capacity. APFS clones,
snapshots, open descriptors and concurrent writes can keep capacity allocated.
The UI always labels physical reclamation unknown.

Launch at login is an explicit native setting. Its status comes from
`SMAppService`; a system approval requirement is shown, never treated as success.
Actual next-login behavior is a separate acceptance check.

## Verification boundaries

Python tests exercise the real broker and collector inside isolated fixtures;
only mount observation is substituted where required by the test filesystem.
Native tests cover parsing and policy. Neither substitutes for opening the
installed status item, checking its data and testing its changed interactions
on the intended Mac. Record real acceptance separately from automated tests.

## Acceptance-driven native-shell revision
The content remains SwiftUI, but the production status icon uses an AppKit
`NSStatusItem` with an explicit target/action and an `NSPopover`. Real-device
AXPress returned success without opening the SwiftUI `MenuBarExtra(.window)`
on the acceptance host; the AppKit shell makes this action explicit and
testable without moving the user's pointer. See the independent upstream
reproduction: https://github.com/jizhi0v0/macos27-beta-issues (entry 34).
No raw-input permission or host-security setting is bypassed.
