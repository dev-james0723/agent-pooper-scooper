# Run a task and collect its new disposable output

`aps run` runs a **noninteractive batch command** and captures its newly generated
standard output and standard error directly through the APS producer broker.
It works independently of Agent Orchestrator. The command receives ordinary
pipes, not broker credentials or managed file paths.

```sh
# The physical parent exists and is controlled by your account.
aps run --run-dir /physical/task-runs/new-run -- python3 -m unittest

# Explicitly authorize collection of this invocation's disposable diagnostic
# output, only when the complete command and capture both succeed.
aps run --run-dir /physical/task-runs/another-new-run --clean-success -- \
  python3 -m unittest
```

The default retains captured output as non-disposable. Use `--clean-success`
only for output you intend to discard, such as successful diagnostic/test logs.
It is a scoped owner instruction for the exact new captured blobs; the wrapper
still creates and validates a fresh exact plan and grant after the registered
grace period. It never approves another run or an existing filesystem object.

**Command-created source files, build trees, caches, agent histories, media and
other native files are outside this operation.** The runner neither adopts nor
deletes them. It does not provide global agent cleanup or a process sandbox.
Existing shell redirects, `tee` files and native session logs also remain
outside it. Removing a captured copy is never reported as removing those files.

## Running and inspecting a task

The run directory must be a **new physical absolute path**. Its parent must
already exist, belong to your account, and not be group/world writable. Existing
runs, symlink leaves and symlink ancestors refuse. On macOS use physical paths;
`/tmp` and `/var` may be symlink aliases. The wrapper creates mode-0700 state and
mode-0600 owner/report files. It never overwrites or automatically removes old
runs. A separate fresh private short socket directory avoids Darwin path limits.

Arguments after `--` are passed literally with no shell. If a command requires a
shell, invoke that shell explicitly and understand its effects. Interactive
stdin is disabled. This does not change the command's ordinary working directory
or environment. The wrapper does not persist command argv, environment values,
or output content in its report; captured output itself remains private in the
managed store when retained. Commands can still have their own independent
effects and logs, as they would outside APS.

Output is mirrored to the caller and captured in bounded immutable chunks. The
default combined ceiling is 16 MiB; `--max-output-bytes` can lower it. Objects
are at most 512 KiB, with at most 33 captured objects at the maximum combined
limit. The command timeout defaults to 3,600 seconds and can be set with
`--timeout` up to 86,400 seconds. Exceeding the output cap terminates the owned
command group and retains the captured prefix. No unbounded spill file is used.

Pipe and terminal mirroring uses nonblocking descriptor writes against the same
command deadline. A caller that stops reading stdout or stderr cannot suspend
that deadline; timed-out capture retains the bytes already observed and skips
collection. Original descriptor flags are restored. The final CLI summary has
its own short bounded write budget and may be omitted when stderr is full;
`report.json` remains available. No background writer thread or deferred Python
output buffer is left behind. Embedded use accepts native Python file streams
or exact `io.BytesIO`/`io.StringIO` memory streams; arbitrary custom `write` or
`flush` callbacks are unsupported because their blocking behavior is unknown.

`report.json` separates the command's actual exit code, capture completion,
collection result, confirmed removed bytes and unknown reclaimed disk capacity.
The owner key, broker ledger, lifecycle journal and report are retained even
after successful collection, so a small task may leave more metadata than the
size of its collected output. This is an audit trail, not a zero-overhead cleaner.

Successful capture means both pipes reached EOF and the command exited. A
descendant keeping a pipe open beyond the bounded drain interval makes capture
incomplete. On timeout/error the wrapper signals only the separate command
process group it created; it does not target unrelated operating-system jobs.
Processes that deliberately leave that group are outside its containment.
The receipt records `command.termination_status` separately from the capture
reason. A denied signal is reported as `permission_denied`, is not retried, and
does not mean the group is confirmed stopped. Output stays retained and the
original incomplete-capture or timeout exit status is preserved.

## Failure and exit status

- A normal command exit code is preserved, including nonzero failures.
- A command signal is represented by `128 + signal number`.
- Timeout returns 124; incomplete/capped/failed capture returns 125; a user
  interruption returns 130.
- Cleanup has a separate status. A command that succeeded still returns zero
  when the collector refuses an unsupported filesystem or stale state; inspect
  the summary and `report.json` for cleanup completion.
- Nonzero command exit, interruption, timeout, output truncation, pending broker
  state or any capture uncertainty never authorizes collection.
- A failure after collection begins may leave partial or unknown effects. The
  report preserves confirmed lower-bound accounting when available. Inspect the
  owner receipts before recovery; do not rerun the command or invent new IDs to
  escape a pending state.

The capture task's managed lifecycle describes the wrapper, not the child
program's business result. A normally observed failed command remains a failed
command in `report.json`, even though the wrapper finished recording its output.

## Verification boundary

Focused tests exercise literal argv, both streams, chunking, limits, timeout,
inherited pipes, failed commands, private outputs, existing/symlink refusal,
broker errors and preservation of unmanaged command-created source/session
files. Fixture collection tests use the real broker and collector with a
controlled mount-observation fixture; installed macOS acceptance separately
checks actual APFS collection. No test proves global native lifecycle fencing,
hostile same-UID isolation, hardware power-loss durability or APFS capacity
reclamation.
