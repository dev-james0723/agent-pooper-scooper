"""User-facing bounded foreground task runner."""
import argparse
import sys
import time

from . import runner


def _summary(text):
    # A full stderr pipe must not re-block after the capture timeout. No Python
    # buffer is filled, so interpreter shutdown has no deferred summary flush.
    try:
        runner._mirror(sys.stderr, (text + "\n").encode(), deadline=time.monotonic() + 0.2)
    except (Exception, KeyboardInterrupt):
        pass  # The private receipt remains authoritative if the sink is full.


def run(args):
    command = list(args.command_argv)
    if command and command[0] == "--":
        command.pop(0)
    try:
        receipt, code = runner.run_command(
            command, args.run_dir, clean_success=args.clean_success,
            timeout=args.timeout, max_output_bytes=args.max_output_bytes,
            stdout=sys.stdout, stderr=sys.stderr)
    except (Exception, KeyboardInterrupt):
        _summary("aps run: refused or interrupted; inspect the selected run directory before retrying")
        return 125
    removed = receipt["cleanup"]["removed"]
    _summary("\naps run: command exit=" + str(receipt["command"]["exit_code"])
          + "; capture=" + receipt["command"]["capture_status"]
          + "; stored=" + str(receipt["capture"]["stored_bytes"]) + " bytes"
          + "; cleanup=" + receipt["cleanup"]["status"]
          + "; confirmed removed=" + str(removed["bytes"]) + " bytes"
          + "; reclaimed capacity unknown; private report: report.json")
    return code


def add_commands(commands):
    parser = commands.add_parser("run", help="run a batch command; capture only its new output in a fresh private store")
    parser.add_argument("--run-dir", required=True, help="new physical absolute directory; parent must already exist")
    parser.add_argument("--clean-success", action="store_true",
                        help="authorize exact collection of this run's disposable output only after complete success")
    parser.add_argument("--timeout", type=float, default=runner.DEFAULT_TIMEOUT, help="command timeout in seconds (default: 3600)")
    parser.add_argument("--max-output-bytes", type=int, default=runner.MAX_OUTPUT_BYTES,
                        help="combined output ceiling, at most 16777216 bytes")
    parser.add_argument("command_argv", nargs=argparse.REMAINDER, help="-- COMMAND ARG ... (literal argv, no interactive stdin)")
    parser.set_defaults(func=run)
