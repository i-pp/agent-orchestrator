"""Focused opt-in routing checks for the clean-HEAD extraction draft."""

from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from mcp_adapter import Adapter, tool_definitions
import scheduler

TASK_ID = '00000000-0000-4000-8000-000000000091'


class SyntheticPipelineProxy:
    def __init__(self):
        self.calls = []

    def dispatch(self, name, arguments):
        self.calls.append((name, arguments))
        return {'task_id': TASK_ID, 'state': 'COMPLETED'}


class CodexPipelineAdapterExtractTests(unittest.TestCase):
    def setUp(self):
        self.runtime_parent = PROJECT / '.runtime'
        self.runtime_parent.mkdir(mode=0o700, exist_ok=True)
        self.runtime_parent.chmod(0o700)
        self.temp = tempfile.TemporaryDirectory(prefix='adapter-extract-', dir=self.runtime_parent)
        self.runtime = Path(self.temp.name)
        (self.runtime / 'logs').mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def test_pipeline_tools_are_not_advertised_by_default(self):
        names = {item['name'] for item in tool_definitions()}
        self.assertFalse(any(name.startswith('codex_') for name in names))
        enabled = {item['name'] for item in tool_definitions(codex_pipeline=True)}
        self.assertIn('codex_task_get', enabled)

    def test_pipeline_proxy_is_required_and_route_is_opt_in(self):
        with self.assertRaisesRegex(ValueError, 'explicitly connected'):
            Adapter(self.runtime, allow_codex_pipeline=True)
        proxy = SyntheticPipelineProxy()
        adapter = Adapter(self.runtime, allow_codex_pipeline=True, pipeline=proxy)
        try:
            initialized = adapter.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'synthetic', 'version': '1'}}})
            self.assertIn('result', initialized)
            adapter.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}})
            listed = adapter.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}})
            self.assertIn('codex_task_get', {item['name'] for item in listed['result']['tools']})
            response = adapter.call_tool('codex_task_get', {'task_id': TASK_ID}, 'synthetic-rpc')
            self.assertFalse(response['isError'])
            self.assertEqual(response['structuredContent']['data']['state'], 'COMPLETED')
            self.assertEqual(proxy.calls, [('codex_task_get', {'task_id': TASK_ID})])
        finally:
            adapter.audit.close()

    def test_resource_profile_changes_only_when_pipeline_is_opted_in(self):
        base = scheduler.configuration()
        enabled = scheduler.configuration(codex_pipeline=True)
        self.assertEqual(base['daemon']['memory_bytes'], 512 * 1024**2)
        self.assertEqual(base['daemon']['pids'], 64)
        self.assertEqual(enabled['daemon']['memory_bytes'], 6 * 1024**3)
        self.assertEqual(enabled['daemon']['pids'], 3072)
        self.assertEqual(scheduler.configuration(), base)


if __name__ == '__main__':
    unittest.main()
