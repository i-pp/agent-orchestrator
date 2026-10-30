"""Synthetic-only detached-start helper for test_poc_services; never hosted OpenAI."""

from pathlib import Path
import json
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from poc_services import Services


class FixtureServices(Services):
    def __init__(self, root, key, base):
        super().__init__(root, sys.executable, key)
        parsed = urlsplit(base)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
            raise ValueError('Only a synthetic loopback plane is permitted')
        self.base = base

    def commands(self):
        commands = super().commands()
        commands['tunnel'] = [('--control-plane.base-url='+self.base if a.startswith('--control-plane.base-url=') else a)
                              for a in commands['tunnel']]
        commands['tunnel'] += ['--control-plane.poll-timeout=1s', '--control-plane.initial-poll-timeout=1s']
        return commands


if __name__ == '__main__':
    # Exercise the actual CLI parser/start/readiness/cleanup with only the
    # network target replaced by a loopback fixture (not a production option).
    import poc_services
    root, key, base = sys.argv[1:]
    poc_services.Services = lambda runtime, codex, key_file: FixtureServices(runtime, key_file, base)
    sys.argv = [sys.argv[0], 'start', '--runtime', root, '--codex', sys.executable, '--key-file', key, '--apply']
    raise SystemExit(poc_services.main())
