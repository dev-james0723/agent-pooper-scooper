# APS safe autonomous cleanup — PRD

## Outcome and authority
The owner requests automatic detection of coding-agent activity and zero-tap cleanup of disposable task output, with a native menu-bar view, tests, review, commit, push and workstation deployment. Implementation and release are authorized. Preserve the existing native menu-bar work and never hand this project back to Agent Orchestrator.

## Current verified baseline
The existing product has bounded native disk inventory, a closed managed-output broker, exact owner-granted collection, and a batch runner. Native process observations are read-only. The runner can collect newly captured stdout/stderr, not arbitrary agent-created files. Native menu-bar work is preserved in this branch; it remains a separately verifiable integration.

## Product requirements
1. Detect recognizable local coding-agent processes automatically without retaining argv, environment, transcript content or secrets. Show detection confidence and unsupported/unknown coverage. Process absence or apparent completion is NEVER deletion authority.
2. Give agents a stable APS-native lifecycle and batch-command entry point. New disposable diagnostics/temp bytes enter APS-owned managed stores at creation. Existing files are not adopted. Source files and primary command output are protected unless explicitly classified disposable.
3. An owner-enabled local autopilot periodically processes only its own enrolled stores. Confirmed successful terminal tasks, expired retention, no references/leases, fresh exact grants and the existing executor are all mandatory. Active, paused, resumable, failed, incomplete, unrecognized and ambiguous states retain bytes.
4. Default retention is 86400 seconds. This is an in-place recovery window, not claimed free disk space. Owner hold/pin and disable controls stop automatic collection. Changing policy must not retroactively shorten an enrolled task's retention. After confirmed unlink, recovery is not promised.
5. Per-host idempotent scheduling and audit receipts: show discovered agents, enrollment coverage, retained/candidate/confirmed removed counts and bytes, last tick, skipped reasons and unknown reclaimed capacity. Never invent capacity savings.
6. Native menu-bar integration may read autopilot status and offer safe pause/status controls; it must distinguish discovered native jobs from APS-managed eligible artifacts. Preserve existing exact manual cleanup confirmation.
7. Install the verified release on both paired Macs using a dedicated low-priority LaunchAgent. Do not replace native agent executables, alter unrelated services, export private machine data, or duplicate AO workers.

## Approaches and decision
A heuristic whole-home sweeper is rejected: neither an idle process nor an old filename proves disposability or safe resumption. Observation-only fails the requested automation. Selected: automatic process discovery plus an owner-enabled policy service over APS-created, lifecycle-fenced managed artifacts; existing protected and unknown native data stays untouched. This is the implementer's safety decision under the owner's full execution approval, not a claim that all historical files are manageable.

## Acceptance
Fresh and installed tests prove a disposable successful task is collected automatically after retention, without per-cleanup approval; active/failed/incomplete/held tasks survive; repeated ticks do not double count; changed identity/symlink/lease/reference/unknown state fail closed. Real APFS acceptance uses only synthetic disposable fixtures. Both hosts must have a read-back service receipt and current runtime status before claiming dual-host deployment. Native histories/worktrees/caches remain explicitly outside deletion coverage. No completion claim for unverified gates.
