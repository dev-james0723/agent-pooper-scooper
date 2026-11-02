# Native Menu Bar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Install a native status-item app backed by real APS data and existing fail-closed collection.
**Architecture:** SwiftUI MenuBarExtra -> bounded local JSON process -> existing Python engine; optional strict SSH snapshot peers.
**Tech Stack:** Swift 5.9+ / SwiftUI / AppKit / ServiceManagement; Python 3.10+ stdlib.
**Spec:** docs/superpowers/specs/2026-11-02-native-menubar-engineering.md

## Global Constraints
macOS 13+; observe-only by default; no native-data cleanup; no public personal paths/secrets; no AO dependency; no automatic remote deletion; no false reclaimed-byte claims; preserve existing branches/worktrees.

## Review Focus
Untrusted/symlink state must refuse, not read elsewhere.
Host changes and late responses must not cross-apply a cleanup.
Stale/active/paused/resumable tasks remain protected.
Timeout or malformed output must not appear as success.
Repeated confirmation must never repeat an uncertain deletion.

### Task 1: JSON gateway
Files: create src/agent_pooper_scooper/menubar.py and tests/test_menubar.py.
Interface: snapshot(state_dir=None)->dict; preview(state_dir,task_id)->dict; collect(state_dir,review_id,confirm=False)->dict.
- [ ] Write tests for absent config, live capacity, no personal paths, malformed config, unknown/active tasks, expiry, exact cleanup and replay.
- [ ] Run `python3 -B -m unittest tests.test_menubar -v`; expect missing module failure initially.
- [ ] Implement bounded private config/receipt reads with existing no-follow helpers; reuse executor, authority and report APIs.
- [ ] Repeat focused tests, then full `python3 -B -m unittest discover -s tests`; expect pass with platform skips explicitly recorded.
- [ ] Commit gateway/tests only after passing.

### Task 2: Native app
Files: create macos/APSMenuBar/Package.swift, Sources/APSMenuBar/*.swift and Tests/APSMenuBarTests/*.swift.
Consumes: snapshot/preview/collect v1 JSON; produces native status panel.
- [ ] Write parsing, stale/future timestamp, peer alias and host-action policy tests; run `swift test -j 2` for red.
- [ ] Implement typed data, bounded Process transport, generation-safe store and 360pt native panel.
- [ ] Add explicit confirmation, history, settings, login toggle and read-only peers.
- [ ] Run Swift tests and release build; inspect compiler output.
- [ ] Commit passing app sources.

### Task 3: Packaging and real acceptance
Files: tools/build_menubar.py, tests/test_build_menubar.py, docs/MENUBAR.md.
Consumes: built Swift binary and verified engine sources; produces signed app and receipt.
- [ ] Write tests rejecting existing/symlink destination and packaging wrong executable or leaking runtime config; run red.
- [ ] Implement bundling without sudo and without overwriting unrelated app/data.
- [ ] Run full Python/Swift suites; build and inspect bundle, signing and source hashes.
- [ ] Obtain independent review of exact changes; fix material findings with regression tests.
- [ ] Run publication gate, commit/push only reviewed SHA under repo draft-PR policy.
- [ ] Install and launch on M3 Max; exercise icon/panel, refresh, review and settings; capture real screenshot.
- [ ] Verify Kingston platform and install same source if compatible; wire only existing authenticated SSH connectivity.
- [ ] Record known facts, source identity, test results and remaining gaps; never relabel mock tests as live acceptance.
