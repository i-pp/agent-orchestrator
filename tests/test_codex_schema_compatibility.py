"""Pinned stable API subset, extracted from installed Codex 0.167.0 schemas.

No CLI execution, network, model call, native verification or enrollment here.
"""
import json
from pathlib import Path
import sys
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from codex_app_protocol import _FIELDS, METHOD_SCHEMAS, schema_version_supported

CONTRACT = json.loads((PROJECT / 'tests/fixtures/codex-0.167-contract.json').read_text())


class Codex0167ContractTests(unittest.TestCase):
    def test_exact_version_admission_not_future_release(self):
        self.assertTrue(schema_version_supported('codex_work_desktop/0.167.0 (Linux)'))
        for value in ('codex/0.167.0-dev', 'codex/0.167.1', 'codex/0.168.0', 'codex/10.167.0'):
            self.assertFalse(schema_version_supported(value))

    def test_exposed_parameter_fields_are_in_generated_stable_schema(self):
        for method, fields in _FIELDS.items():
            with self.subTest(method=method):
                self.assertLessEqual(fields, set(CONTRACT['methods'][method]['properties']))

    def test_generated_required_parameters_are_enforced(self):
        for method, value in METHOD_SCHEMAS.items():
            with self.subTest(method=method):
                self.assertLessEqual(set(CONTRACT['methods'][method]['required']), set(value[2]))

    def test_only_inspected_single_shot_decisions_and_resolution_types(self):
        for name in ('CommandExecutionRequestApprovalResponse.json', 'FileChangeRequestApprovalResponse.json'):
            self.assertEqual(CONTRACT['methods'][name]['required'], ['decision'])
            self.assertEqual(CONTRACT['methods'][name]['validated_decisions'], ['accept', 'decline'])
        resolved = CONTRACT['methods']['serverRequest/resolved']
        self.assertEqual(set(resolved['required']), {'requestId', 'threadId'})
        self.assertEqual(resolved['validated_id_types'], ['integer', 'string'])

    def test_evidence_is_versioned_bounded_and_stable_only(self):
        self.assertEqual(CONTRACT['version'], '0.167.0')
        self.assertFalse(CONTRACT['experimental_api'])
        self.assertEqual(CONTRACT['validated_examples'], 17)
        for value in CONTRACT['methods'].values():
            self.assertRegex(value['sha256'], r'^[0-9a-f]{64}$')


if __name__ == '__main__':
    unittest.main()
