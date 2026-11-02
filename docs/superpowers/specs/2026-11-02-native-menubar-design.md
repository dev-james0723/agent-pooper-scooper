# Agent Pooper Scooper Native Menu Bar — PRD

Status: approved direction from owner; implementation and acceptance pending.

## Intent
Replace a dashboard-first experience with a quiet native macOS status icon. Clicking it opens a compact panel with real disk capacity, managed-task accounting, safety review, history and settings. It must work independently of AO and preserve all existing work.

## Product contract
- macOS 13+; SwiftUI MenuBarExtra with window style, no Electron, web dashboard or persistent Dock icon.
- A 360-point-wide panel with Overview, Review, History and Settings. Respect system appearance, keyboard navigation, reduced motion and accessible control labels.
- Real capacity with its timestamp and volume; inventory has a separate age/completeness indicator. Never use illustrative metrics in production.
- Native agent sessions are NOT inferred from processes or directory names. Only explicitly configured APS managed tasks appear; missing integration is an explicit empty state, not zero activity.
- Review shows exact eligible managed objects, retained counts and unknown reclamation. Cleanup requires a fresh explicit confirmation bound to a stored exact plan; never auto-approve on opening the panel.
- Cleanup of native files, source, sessions, worktrees, models, downloads, media, VMs and shared caches is unsupported. Existing safety policy remains authoritative.
- Per-host selection uses the local machine and optional preconfigured SSH peers. Remote inspection is read-only. Authentication failures/offline/stale data are visible; remote cleanup is not exposed.
- Refresh is coalesced and bounded. Idle panel does not repeatedly walk the filesystem. Existing monitor handles scheduled low-disk notices.
- Launch at login is explicit and reflects actual SMAppService status. No repeated notification spam.

## Approaches
Selected: native SwiftUI view plus a versioned JSON gateway over the existing Python engine. Reuses its broker and safety checks; no local HTTP port. Electron and a browser dashboard conflict with the owner's explicit direction. Rewriting the collector in Swift would duplicate a safety-critical implementation and is rejected.

## Primary journey
Click status icon -> read live local capacity -> review enrolled tasks -> request exact plan -> inspect objects/bytes and protected-data explanation -> explicitly confirm eligible local collection -> read durable result. History distinguishes removed file bytes from unknown physical SSD reclamation. Switching host invalidates pending review.

## Release boundaries
A locally installed ad-hoc-signed preview is not a notarized public release. No claim of complete native Claude/Codex/AO integration or automatic cross-Mac discovery. Existing account/SSH configuration is reused, never weakened. No production cleanup during acceptance except isolated deliberately created disposable fixtures.

## Acceptance
Build native binary; validate Python unit suite and new gateway tests; prove unknown/protected files and active/resumable tasks remain intact; fail stale/malformed/replayed cleanup requests safely; install without overwriting unrelated apps; inspect actual status icon/panel and capture screenshots on a real Mac; verify local data and optional peer connectivity. Record exact source/build identity and all unverified items.

## Acceptance-driven native-shell revision
The content remains SwiftUI, but the production status icon uses an AppKit
`NSStatusItem` with an explicit target/action and an `NSPopover`. Real-device
AXPress returned success without opening the SwiftUI `MenuBarExtra(.window)`
on the acceptance host; the AppKit shell makes this action explicit and
testable without moving the user's pointer. See the independent upstream
reproduction: https://github.com/jizhi0v0/macos27-beta-issues (entry 34).
No raw-input permission or host-security setting is bypassed.
