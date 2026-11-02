"""Public-safe controls and literal command capture for the local autopilot."""
import argparse
import json
import sys

from . import agent_observer, autopilot, autopilot_tasks, runner


def _run(args):
    try:
        action = args.autopilot_action
        code = 0
        if action == 'init':
            value = autopilot.initialize(args.state_dir, retention_seconds=args.retention_seconds)
        elif action in ('enable', 'disable'):
            value = autopilot.set_enabled(args.state_dir, action == 'enable')
        elif action in ('hold', 'release'):
            value = autopilot.set_hold(args.state_dir, args.enrollment_id, action == 'hold')
        elif action == 'tick':
            value = autopilot.tick(args.state_dir)
        elif action == 'run':
            argv = list(args.command_argv)
            if argv and argv[0] == '--':
                argv.pop(0)
            receipt, code = autopilot_tasks.run(args.state_dir, argv, disposable_output=args.disposable_output,
                                               timeout=args.timeout, max_output_bytes=args.max_output_bytes,
                                               stdout=sys.stdout if args.stream_output else None,
                                               stderr=sys.stderr if args.stream_output else None)
            value = {'enrollment_id': receipt['enrollment_id'], 'command': receipt['command'],
                     'capture': {k: receipt['capture'][k] for k in ('stored_bytes', 'complete')},
                     'cleanup': receipt['cleanup']['status'], 'reclaimed_bytes': None}
        else:
            value = autopilot.status(args.state_dir)
            value['discovery'] = agent_observer.observe_agents()
        if action == 'run' and args.stream_output:
            from .run_cli import _summary
            _summary(json.dumps(value, allow_nan=False, ensure_ascii=True))
        else:
            print(json.dumps(value, allow_nan=False, ensure_ascii=True))
        return code
    except (Exception, KeyboardInterrupt):
        print(json.dumps({'error': 'Autopilot refused or outcome uncertain; inspect private receipts before retrying.'}))
        return 1


def add_commands(commands):
    parser = commands.add_parser('autopilot', help='new APS capture only; native discovery grants no cleanup authority')
    actions = parser.add_subparsers(dest='autopilot_action', required=True)
    for name in ('init', 'enable', 'disable', 'status', 'tick', 'run', 'hold', 'release'):
        command = actions.add_parser(name)
        command.add_argument('--state-dir', required=True)
        command.set_defaults(func=_run)
        if name == 'init':
            command.add_argument('--retention-seconds', type=int, default=86400)
        if name in ('hold', 'release'):
            command.add_argument('--enrollment-id', required=True)
        if name == 'run':
            command.add_argument('--disposable-output', action='store_true')
            command.add_argument('--stream-output', action='store_true',
                                 help='explicitly mirror private child output; bounded summary goes to stderr')
            command.add_argument('--timeout', type=float, default=runner.DEFAULT_TIMEOUT)
            command.add_argument('--max-output-bytes', type=int, default=runner.MAX_OUTPUT_BYTES)
            command.add_argument('command_argv', nargs=argparse.REMAINDER)
