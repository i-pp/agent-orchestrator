"""Security contracts for the small shared private-state helper extraction."""

import os
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from private_state import PrivateStateError, _json, _private


class PrivateStateExtractTests(unittest.TestCase):
    def test_json_rejects_duplicate_members_and_non_finite_values(self):
        for raw in [b'{"a":1,"a":2}', b'{"a":NaN}']:
            with self.subTest(raw=raw), self.assertRaisesRegex(PrivateStateError, 'INVALID_PRIVATE_STATE_JSON'):
                _json(raw)

    def test_private_file_checks_owner_mode_links_and_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private.json'
            path.write_bytes(b'{"ok":true}')
            path.chmod(0o600)
            _private(path, 32)
            with self.assertRaisesRegex(PrivateStateError, 'UNSAFE_PRIVATE_STATE_FILE'):
                _private(path, 4)
            path.chmod(0o640)
            with self.assertRaisesRegex(PrivateStateError, 'UNSAFE_PRIVATE_STATE_FILE'):
                _private(path)

    def test_private_file_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            target.write_text('x')
            target.chmod(0o600)
            link = Path(directory) / 'link'
            link.symlink_to(target)
            with self.assertRaisesRegex(PrivateStateError, 'UNSAFE_PRIVATE_STATE_FILE'):
                _private(link)


if __name__ == '__main__':
    unittest.main()
