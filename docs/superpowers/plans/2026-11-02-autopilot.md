# APS Safe Autopilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Automatic local agent discovery and owner-enabled, retention-bound cleanup of new APS-managed disposable output.
**Architecture:** A low-priority per-user periodic tick observes agent presence and serializes a private enrollment ledger. Only the existing managed authority/executor can remove enrolled artifacts; native observations cannot authorize deletion.
**Tech Stack:** Python >=3.10 standard library, existing SQLite authority, macOS LaunchAgents, existing native menu-bar app.
**Spec:** `docs/superpowers/specs/2026-11-02-autopilot-design.md` and `docs/superpowers/specs/2026-11-02-autopilot-engineering.md`.

## Global Constraints
No runtime dependencies; no arbitrary native deletion; default retention 86400 seconds; unknown/active/paused/resumable/failed stays retained; private state never published; preserve other worktrees and APS UI work. Direct local execution only, no AO scheduling.

## Review Focus
- Symlink/hardlink/ancestor replacement and registry tampering must not redirect deletion.
- Interrupted enrollment/capture/collection must stay unknown and cannot gain eligibility through a retry.
- Concurrent disable/hold and ticks cannot create stale automatic grants.
- Failed commands, oversized output and paused/resumable broker state remain protected despite plausible process metadata.
- Public reports and publication must not reveal paths, argv, credentials or unrelated workspace information.

### Task 1: Discovery and strict private policy state
**Files:** create `src/agent_pooper_scooper/agent_observer.py`, `autopilot.py`; tests `tests/test_agent_observer.py`, `tests/test_autopilot.py`.
**Interfaces:** observer returns sanitized provider/count/confidence with no authority; initialize/set_enabled/status own an authenticated marked private directory.
- [x] Write tests for known/unknown processes, timeout, bounded malformed output and argv redaction; run RED.
- [x] Write state tests for create-only, exact schemas, bool/NaN/oversize, unsafe paths and lock contention; run RED.
- [x] Implement minimal interfaces and run focused tests GREEN.
- [ ] Full suite GREEN: sandbox refuses Unix socket binding; baseline supplied by supervisor passed.
- [x] Ledger task evidence privately.
- [ ] Commit task code/tests: shared Git worktree metadata is outside writable sandbox roots.

### Task 2: Managed task enrollment and deferred capture
**Files:** create `autopilot_tasks.py`, `tests/test_autopilot_tasks.py`; minimally modify `runner.py`, extend `tests/test_runner.py`.
**Interfaces:** run(state_dir, literal_argv, disposable_output, timeout, max_output_bytes) returns known receipt/exit status; enrollment identifiers cannot be filesystem paths.
- [x] RED tests: enrollment precedes command; successful diagnostic output retained until policy deadline; failed/incomplete/default-protected output never eligible; foreign roots cannot enroll.
- [x] Implement reusing existing broker/capture, exact creation-time grace and durable intent. Preserve all existing runner defaults.
- [x] GREEN focused tests.
- [ ] Full suite GREEN and local commit (sandbox socket/Git metadata blockers).

### Task 3: Autonomous collector, CLI and receipts
**Files:** implement tick/hold in `autopilot.py`; create `autopilot_cli.py`, `tests/test_autopilot_cli.py`; register in `cli.py`.
**Interfaces:** tick makes fresh exact plan/grant only for enrolled successful disposable runs, through existing executor, with permanent operation identity and lower-bound accounting.
- [x] RED tests for auto collection after retention, repeated-tick no double count, reference/lease/resume/hold/disable/failed protections, stale revisions, symlink changes, pending effects and redaction.
- [x] Implement the bounded serialized tick and explicit safe CLI. No automatic repair/regrant after ambiguity.
- [x] GREEN focused tests and literal-argv CLI contract smoke.
- [ ] Installed broker smoke, full suite GREEN and commit (sandbox blockers).

### Task 4: Installation, UI status, documentation and agent skill
**Files:** `tools/install_autopilot.py`, `tests/test_install_autopilot.py`, `docs/AUTOPILOT.md`, skill markdown; additive native UI status only as supported by existing gateway.
- [x] RED installer tests: absolute executable, exact service label, interval60, owner state, idempotency, unrelated plist untouched, existing unknown output refused.
- [x] Implement and document service and rollback; docs explicitly distinguish discovery from enrollment. Add automatic status to UI without weakening manual cleanup confirmation; test Python DTO and native model when changed.
- [x] Build native app directly with swiftc; run native model smoke (10 assertions).
- [x] Build 0.2.0a1 wheel and install offline in a new private venv; verify installed CLI and no-start installer idempotence with synthetic monitor preservation.
- [ ] SwiftPM tests (nested sandbox denied), installed APFS broker acceptance (socket bind denied), full Python GREEN, immutable implementation publication gate and local release commit (shared Git metadata denied).

Implementation is present in this feature worktree. No push, merge, service deployment, sibling source mutation or personal-data cleanup was performed. The supervising caller must review and commit the actual changes before treating any artifact as an immutable release. The existing HEAD publication check fails inherited non-noreply commit metadata; no history was rewritten. Raw evidence is in the requested private engineering ledger.

### Task 5: Independent review, publication and physical-host acceptance
- [ ] Review exact branch SHA independently for public privacy, code correctness and all Review Focus cases; resolve material findings with regression RED/GREEN.
- [ ] Build verified immutable release, publish reviewed branch/draft PR, wait for all CI checks, merge only under owner's release approval and applicable repository protection.
- [ ] Install on primary then secondary Mac; preserve known prior release targets and existing monitor/UI. Enable periodic service and install dedicated APS skill without altering native executable shims.
- [ ] Run actual APFS synthetic successful auto-clean/held/failed/repeated-tick fixtures; inspect both launchd services and current runtime status. Record private receipts and report exact verification limits.
