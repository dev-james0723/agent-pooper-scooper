# Native Menu Bar — Engineering Specification

## Components
`src/agent_pooper_scooper/menubar.py`: bounded JSON gateway (snapshot, preview, collect), used as `python -m agent_pooper_scooper.menubar`. Configuration and review journals live in an owner-private app-state directory, never the public repository.
`macos/APSMenuBar/Package.swift`: dependency-free Swift package, macOS 13, executable APSMenuBar.
`macos/APSMenuBar/Sources/APSMenuBar/`: typed JSON models, bounded process transport, MainActor store, native views and login integration.
`tools/build_menubar.py`: reproducible bundle packaging, exact embedded engine source, ad-hoc signing and install into an explicitly selected absent destination. No root privileges.

## Data protocol v1
Snapshot contains schema/version, observed_at, local host name, volume capacity, inventory status/age, managed task summaries, retained/eligible/removed bytes, history, limitations, and peer definitions. No command text, file contents, capabilities or personal paths are returned. Missing or invalid data stays unknown.
Config is an optional mode-0600 owned regular file under a mode-0700 private directory. Maximum 64 KiB, maximum 12 managed tasks and 4 SSH peers. Task entries contain id, label, store, owner_key_file, task_id. Peers contain label and restricted SSH alias. No automatic adoption of native directories.
Snapshot operations only read authenticated managed state and capacity; they never grant collection. Each task failure is isolated and labelled unavailable.
Preview persists an immutable exact executor plan and opaque random review ID, scope/task and creation time in a new private file. UI sees only the review ID, candidate count/bytes, retained information, short expiry and limitations.
Collect requires an explicit confirmation, local-host selection and a valid unexpired review. A private exclusive receipt file is reserved BEFORE any effect. Existing receipt yields its known outcome or an ambiguous refusal, never duplicate effects. The collector grants only the saved exact plan and uses the existing fresh broker checks. No raw filesystem deletion is implemented in this feature. Remote mode allows snapshot only.

## Swift process transport
Launch embedded gateway with argv, not a user-composed shell command. Read bounded stdout off the main thread; one request in flight. Deadline and output ceiling produce an explicit failure, not inferred success. Do not kill unrelated processes. SSH is batch-only with strict host key checking and bounded connect time; only validated host aliases, a constant remote command and snapshot action are accepted. Selection changes invalidate reviews; requests are generation-bound so late results cannot overwrite another host.

## UI
Native system fonts and semantic colours; icon is a monochrome symbol with accessible Agent Pooper Scooper label. 360pt panel with sufficient minimum height, no fake chart. Capacity primary; eligible managed bytes secondary. Empty/error/partial/offline/refreshing states are real. Tabs retain no unsafe pending action across host changes. Confirmation states deletion is irreversible and disk reclamation unknown. History contains bounded latest local collection receipts. Settings displays launch-at-login status and integration limitations.

## Testing and delivery
TDD for JSON contracts, missing/malformed/private-state validation, symlink refusal, active/resumable retention, plan expiry, exact approval, replay suppression and no native deletions. Native package tests cover parsing, freshness, aliases and UI state policy. Run the existing full Python suite, Swift tests/build and public immutable-object gate. Fresh-context review precedes public push. Keep machine-specific acceptance screenshots/logs outside public Git. Install and launch locally after checks; capture real panel and test changed interaction. No merge bypass, no signing identity invention, no notarization claim.

## References
Apple MenuBarExtra: https://developer.apple.com/documentation/swiftui/menubarextra
Apple SMAppService: https://developer.apple.com/documentation/servicemanagement/smappservice
Existing docs/SAFETY_MODEL.md, docs/EXECUTION_SAFETY.md and docs/TASK_RUNNER.md remain authoritative.

## Acceptance-driven native-shell revision
The content remains SwiftUI, but the production status icon uses an AppKit
`NSStatusItem` with an explicit target/action and an `NSPopover`. Real-device
AXPress returned success without opening the SwiftUI `MenuBarExtra(.window)`
on the acceptance host; the AppKit shell makes this action explicit and
testable without moving the user's pointer. See the independent upstream
reproduction: https://github.com/jizhi0v0/macos27-beta-issues (entry 34).
No raw-input permission or host-security setting is bypassed.
