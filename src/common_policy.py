"""Verified common-policy snapshots and transport-neutral context assembly.

This module performs no model calls, network access, writes, or authorization.
Project configuration is operator-owned input, not a task/MCP parameter.
"""

import hashlib
import json
from pathlib import Path
import re
import stat
from typing import NamedTuple


class PolicyError(ValueError):
    """The common policy could not be loaded or matched to its pinned version."""


class PolicyBundle(NamedTuple):
    text: str
    version: str
    sha256: str
    source: str

    def metadata(self):
        return {'version': self.version, 'sha256': self.sha256, 'source': self.source}


class SourcePolicyBundle(NamedTuple):
    text: str
    version: str
    sha256: str
    source: str
    provenance_json: str

    def metadata(self):
        return {'version':self.version,'sha256':self.sha256,'source':self.source,
                'upstream':json.loads(self.provenance_json)}


_VERSION = re.compile(r'[0-9]+(?:\.[0-9]+)*\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_TITLE = '# AI開発オーケストレーション統合指示 v'


def _path(path, *, directory=False):
    try:
        raw = Path(path).expanduser().absolute()
        # Check the lexical ancestors before resolve: resolving first would hide
        # an operator-supplied symlink, including one followed by '..'.
        for item in (raw, *raw.parents):
            if item.is_symlink():
                raise PolicyError('Symlink policy path is not allowed: ' + str(item))
        resolved = raw.resolve(strict=True)
        mode = resolved.stat().st_mode
        expected = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
        if not expected:
            raise PolicyError('Expected policy ' + ('directory: ' if directory else 'file: ') + str(resolved))
        return resolved
    except PolicyError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise PolicyError('Policy path is unavailable: ' + str(path)) from error


def _bytes(path):
    try:
        return _path(path).read_bytes()
    except OSError as error:
        raise PolicyError('Policy file could not be read: ' + str(path)) from error


def _utf8(raw, path):
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError as error:
        raise PolicyError('Policy file is not UTF-8: ' + str(path)) from error


def _json(path, keys):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PolicyError('Duplicate JSON key: ' + key)
            result[key] = value
        return result

    def invalid_constant(value):
        raise PolicyError('Non-finite JSON value is not allowed')

    try:
        value = json.loads(_utf8(_bytes(path), path), object_pairs_hook=unique,
                           parse_constant=invalid_constant)
    except json.JSONDecodeError as error:
        raise PolicyError('Invalid policy JSON: ' + str(path)) from error
    valid = [set(k) for k in keys] if isinstance(keys,tuple) else [set(keys)]
    if not isinstance(value, dict) or set(value) not in valid:
        raise PolicyError('Unexpected or missing policy fields: ' + str(path))
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise PolicyError('Unsupported policy schema: ' + str(path))
    return value


def _version(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise PolicyError('Invalid policy version')
    return value


def _sha256(value):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise PolicyError('Invalid policy SHA-256')
    return value


def _heading(text, version):
    if not text.splitlines() or text.splitlines()[0] != _TITLE + version:
        raise PolicyError('Policy heading does not match the manifest version')


def load_policy(bundle_root, *, expected_version=None, expected_sha256=None):
    """Read an exact UTF-8 snapshot after checking the manifest and caller pins."""
    root = _path(bundle_root, directory=True)
    manifest = _json(root / 'policies/manifest.json',
                     {'schema_version', 'version', 'file', 'sha256'})
    version, wanted_hash = _version(manifest['version']), _sha256(manifest['sha256'])
    if manifest['file'] != 'policies.md':
        raise PolicyError('The common-policy canonical file must be policies.md')
    if expected_version is not None and _version(expected_version) != version:
        raise PolicyError('Policy version conflicts with the caller pin')
    if expected_sha256 is not None and _sha256(expected_sha256) != wanted_hash:
        raise PolicyError('Policy SHA-256 conflicts with the caller pin')
    source = _path(root / 'policies.md')
    raw = _bytes(source)
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != wanted_hash:
        raise PolicyError('Policy content differs from the manifest SHA-256')
    text = _utf8(raw, source)
    _heading(text, version)
    return PolicyBundle(text, version, actual_hash, str(source))


def load_project_policy(project_root):
    """Load the common bundle selected and pinned by a project's operator file."""
    root = _path(project_root, directory=True)
    fields={'schema_version','bundle_root','expected_version','expected_sha256'}
    config = _json(root / 'config/policy.json',(fields,fields|{'upstream_pin'}))
    location = config['bundle_root']
    if not isinstance(location, str) or not location.strip() or Path(location).is_absolute():
        raise PolicyError('bundle_root must be a nonempty project-relative path')
    legacy=load_policy(root / location,
                       expected_version=_version(config['expected_version']),
                       expected_sha256=_sha256(config['expected_sha256']))
    if 'upstream_pin' in config:
        if config['upstream_pin']!='upstream-policy.json':raise PolicyError('Unexpected upstream pin path')
        from upstream_policy import load_upstream
        return load_upstream(root,legacy)
    if (root/'config/upstream-policy.json').exists():
        raise PolicyError('Upstream pin exists but project source selection is missing')
    return legacy


def _checked_bundle(bundle):
    if isinstance(bundle,SourcePolicyBundle):
        from upstream_policy import check_bundle
        check_bundle(bundle)
        return
    if not isinstance(bundle, PolicyBundle) or not isinstance(bundle.text, str):
        raise PolicyError('A loaded PolicyBundle is required')
    _version(bundle.version)
    _sha256(bundle.sha256)
    try:
        raw = bundle.text.encode('utf-8')
    except UnicodeEncodeError as error:
        raise PolicyError('PolicyBundle text is not valid UTF-8') from error
    if hashlib.sha256(raw).hexdigest() != bundle.sha256:
        raise PolicyError('PolicyBundle content hash is inconsistent')
    _heading(bundle.text, bundle.version)
    if not isinstance(bundle.source, str) or not bundle.source:
        raise PolicyError('PolicyBundle source is missing')


def developer_instructions(bundle, role_instructions=''):
    """Include the verified source verbatim; supplementary role text is separate."""
    _checked_bundle(bundle)
    if not isinstance(role_instructions, str):
        raise PolicyError('Role instructions must be text')
    if bundle.text in role_instructions:
        raise PolicyError('Role instructions must not duplicate the common-policy body')
    metadata = json.dumps(bundle.metadata(), ensure_ascii=False, sort_keys=True)
    preamble = (
        '共通ポリシーの検証済みスナップショットを以下に含む。\n'
        '適用順位: 実行環境の上位規則・権限・ユーザーの明示的制約が優先する。'
        'このポリシーは権限や承認を追加しない。'
        '既存の安全ガード・追加隔離禁止・明示されたscope/認証/費用制限を上書きしない。'
        '共通ポリシーはその範囲内で適用し、ロール/実行の補足は本文を変更しない。\n'
        'Policy metadata: ' + metadata + '\n\n--- BEGIN COMMON POLICY ---\n')
    boundary = '' if bundle.text.endswith('\n') else '\n'
    return (preamble + bundle.text + boundary + '--- END COMMON POLICY ---\n\n'
            '--- ROLE / EXECUTION INSTRUCTIONS ---\n' + role_instructions)


def chat_messages(bundle, task, role_instructions=''):
    """Assemble OpenAI Chat-compatible messages; never send them."""
    if not isinstance(task, str):
        raise PolicyError('Task must be text')
    return [{'role': 'system', 'content': developer_instructions(bundle, role_instructions)},
            {'role': 'user', 'content': task}]


def context_packet(bundle, task, target):
    """Build an explicit body-bearing handoff, not a link or delivery receipt."""
    if target not in ('chatgpt', 'dot', 'codex', 'local'):
        raise PolicyError('Unknown policy context target')
    if not isinstance(task, str):
        raise PolicyError('Task must be text')
    return {'schema_version': 1, 'target': target, 'policy': bundle.metadata(),
            'developer_instructions': developer_instructions(bundle), 'task': task}
