---
name: aps-autopilot
description: Use when running reproducible batch diagnostics on an APS-enabled workstation or inspecting, holding, or pausing managed cleanup.
---

# APS disposable diagnostics

Use the owner's verified installed APS CLI and explicit physical autopilot state
directory. Do not initialize or enable policy unless the owner authorizes it.
Inspect `aps autopilot status --state-dir STATE` first. Native discovery counts
do not mean existing agent jobs or files are enrolled.

For replaceable diagnostic stdout/stderr only:

```sh
aps autopilot run --state-dir STATE --stream-output --disposable-output -- COMMAND ARG
```

`--stream-output` lets the caller see private diagnostics; never publish that output.
Omit it when a sanitized JSON receipt is required.

Use literal argv, not a shell string. Keep primary results, failed output,
sessions, source, transcripts, worktrees, credentials and caches protected.
Omit `--disposable-output` for primary/default output. Do not adopt old files.
This wrapper captures new pipe bytes only; child-created files are unenrolled.

Default recovery retention is 24 hours (86400 seconds). Collection requires
confirmed successful capture, terminal broker generation, expired retention,
no references/leases, a fresh exact grant and confirmed executor receipts.
Process disappearance never grants deletion authority. There is no recovery
promise after confirmed unlink, and reclaimed physical space remains unknown.

Use `hold --enrollment-id ID`, `release --enrollment-id ID`, or `disable` under
`aps autopilot`, always with `--state-dir STATE`. A busy/refused policy operation
has not taken effect; inspect status. Do not retry uncertain collection, change
operation IDs, re-enroll pending output or edit private state. Preserve receipts
for the owner. Never publish paths, argv, captured bytes, keys or private logs.

The dedicated per-user scheduler runs one bounded tick every 60 seconds.
Installation and real service changes belong to the owner/release operator.
Do not replace executable shims or rewrite unrelated settings to enroll agents.
