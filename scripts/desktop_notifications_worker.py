#!/usr/bin/env python3
"""Run only local desktop delivery, without webhook credentials or task execution."""
import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))

from desktop_notifications import DesktopNotificationQueue, FreedesktopSender
from notification_outbox import NotificationError
from runtime_scope import resolve_project_root, runtime_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--runtime-project-root', type=Path, required=True,
                        help='Explicit operator-owned project containing the private runtime')
    parser.add_argument('--status', action='store_true',
                        help='Read an existing queue without sending or changing its state')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        project = resolve_project_root(args.runtime_project_root)
        root = runtime_path(args.runtime, project_root=project)
        destination = root / 'desktop-notifications.sqlite3'
        if args.status:
            print(json.dumps(DesktopNotificationQueue.read_summary(destination), sort_keys=True))
            return 0
        desktop = DesktopNotificationQueue(root / 'codex-pipeline.sqlite3', destination)
        desktop.initialize()
    except (NotificationError, OSError, ValueError, TypeError, sqlite3.Error):
        print('Desktop worker startup refused: DESKTOP_CONFIGURATION_INVALID', file=sys.stderr)
        return 1

    stopped = threading.Event()

    def request_stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    sender = FreedesktopSender()
    print('Desktop worker ready', flush=True)
    had_error = False
    while not stopped.is_set():
        try:
            desktop.run_once(sender)
            had_error = False
        except Exception:
            if not had_error:
                print('Desktop notification poll failed: DESKTOP_POLL_FAILED',
                      file=sys.stderr, flush=True)
            had_error = True
        stopped.wait(1.0)
    print('Desktop worker stopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
