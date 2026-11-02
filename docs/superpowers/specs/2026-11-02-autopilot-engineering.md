# APS safe autonomous cleanup — engineering specification

## Invariants
Python >=3.10, standard-library runtime only. Reuse ManagedAuthority and executor APIs; do not bypass collection fences, modify their SQL, or convert snapshots into grants. New state must be physically addressed, owner-only 0700 directories and 0600 regular single-link files, no symlink ancestors/leaves, bounded JSON, atomic replace and fsync. Keep private runtime evidence outside the public repository. Preserve the existing runner defaults and menu-bar behavior.

## Modules and interfaces
- `agent_observer.py`: `observe_agents() -> dict`; bounded subprocess output/deadline, same-UID process projections only; use explicit known executable names/scripts, never substring-match arbitrary commands. Return provider/count/confidence and `cleanup_authority: none`; do not persist raw command lines, paths or process content. Unsupported/malformed/timeout is unknown, never zero-as-proof.
- `autopilot.py`: private marked state initialization, owner policy, enrollment, task holds, serialized tick and cumulative receipts. `initialize(state_dir, retention_seconds=86400, enabled=False)`, `set_enabled`, `status`, `set_hold`, `tick`. Exact schema validation and all numeric bounds exclude bool/NaN/infinity. Explicit initialization refuses existing unknown directories.
- `autopilot_tasks.py`: create-only enrollment and a batch-run entry point using the existing capture/broker, preserving its timeout/output-cap/failure semantics. Records pending enrollment before effects; confirmed successful completion before eligibility. No broad path-registration interface and no adoption of existing task outputs. Add a minimal managed emit/lifecycle API only when it reuses the existing broker without weakening scope.
- `autopilot_cli.py`: `aps autopilot init|enable|disable|status|tick|run|hold|release`; explicit `--state-dir`, JSON public-safe status, controlled errors. `run` requires `--disposable-output` to permit automatic cleanup, otherwise retain. Literal argv after `--`, no interactive stdin and no shell injection. Returning command success must not hide a cleanup failure.
- `runner.py`: only minimal backward-compatible changes needed to defer cleanup while still recording disposable new output and exact creation-time retention. All existing tests remain green. Never mark failed or incomplete runs eligible.
- `tools/install_autopilot.py`: first-create dedicated LaunchAgent, `RunAtLoad`, `StartInterval=60`, `Nice=10`, bounded single tick, absolute verified installed executable, no shell. Preserve existing monitor and native UI installations. Refuse unexpected existing destinations; support an idempotent verified reinstall/status path. No sudo.
- `docs/AUTOPILOT.md` and a distributable agent skill: show correct diagnostic-only usage, how to inspect/hold/disable, install/rollback, and the difference between automatic native discovery and enrolled cleanup. Do not claim native transcript/cache/worktree reclamation.

## Limits and accounting
At most 128 enrolled runs initially, 16 MiB combined captured bytes/run, 64 artifacts/collection, <=16 runs/tick, <=30 seconds per owned query/collection phase. No unconstrained filesystem recursion. Persist a per-tick lock and per-enrollment intent before execution. Never retry ambiguous collection with new operation IDs; use existing receipts to report confirmed lower bounds, fail closed. Collector removed bytes are exact unlink receipts; reclaimed capacity is null. Keep metadata and tombstones; saturation is an explicit refusal, not silent deletion. Hold, disable and collection are serialized against policy changes.

## Installation and release
Build an immutable wheel, install offline into a new versioned directory, smoke from outside source, verify exact version and hash, then switch only known APS entrypoints/services with a recoverable prior target. Never delete the old release or state. Install agent usage guidance through a dedicated APS skill only; do not patch unrelated global instructions or executable shims. Validate both actual hosts independently. Publish only independently reviewed SHA after publication gate and CI. Production smoke must include zero-tap safe collection, retain cases, repeated-tick accounting and service readback. A real automatic tick is not evidence that unrelated agents are enrolled.

## Primary references
- https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
- https://docs.python.org/3.10/library/os.html
- Existing `docs/SAFETY_MODEL.md`, `docs/EXECUTION_SAFETY.md`, `docs/TASK_RUNNER.md` are binding. This feature does not weaken native-data protection.
