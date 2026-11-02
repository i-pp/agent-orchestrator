"""Bounded legacy/2026 MCP stdio facade; no scheduler, model, or TCP listener."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import sqlite3
import sys
import subprocess
import time
import uuid

from control_socket import request
from storage import STATES
from verification import normalize_acceptance
from workspaces import spec
from chat_mailbox import Mailbox, MailboxError, TOOLS as MAILBOX_TOOLS, validate_arguments as mailbox_arguments
from codex_pipeline import (TOOLS as PIPELINE_TOOLS, PipelineError,
                            tool_definitions as pipeline_tools, validate_arguments as pipeline_arguments)
from experiments import (TOOLS as EXPERIMENT_TOOLS, EvidenceError, ExperimentStore,
                         tool_definitions as experiment_tools, validate_arguments as experiment_arguments)

PROJECT = Path(__file__).resolve().parents[1]
PROTOCOL = '2025-11-25'
REQUEST_PROTOCOL = '2026-07-28'
VERSION_META = 'io.modelcontextprotocol/protocolVersion'
CAPABILITIES_META = 'io.modelcontextprotocol/clientCapabilities'
CLIENT_META = 'io.modelcontextprotocol/clientInfo'
SERVER_META = 'io.modelcontextprotocol/serverInfo'
SERVER_INFO = {'name': 'agent-orchestrator-local', 'version': '0.2.0'}
MAX_INPUT = 65536
MAX_OUTPUT = 65536
AUDIT_LIMIT = 4 * 1024**2
TIMEOUT = 2
KEY_PATTERN = r'^[A-Za-z0-9_.:-]{1,128}$'
UUID_PATTERN = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'


class Invalid(ValueError):
    pass


def decode(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result: raise ValueError('Duplicate JSON member')
            result[k] = v
        return result
    value = json.loads(raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))
    # Reject overflowed numeric literals and unpaired escaped surrogates too.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    return value


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise Invalid('Unexpected or missing fields')


def uuid_value(value):
    return value if isinstance(value, str) and re.fullmatch(UUID_PATTERN, value) else None


def runtime_path(root):
    raw = Path(root).expanduser().absolute()
    if any(p.is_symlink() for p in [raw, *raw.parents]): raise ValueError('Unsafe runtime ancestry')
    resolved = raw.resolve(strict=True)
    if not resolved.is_relative_to(PROJECT / '.runtime'): raise ValueError('Runtime must be inside project .runtime')
    for p in [resolved, resolved / 'logs']:
        s = p.stat()
        if p.is_symlink() or not p.is_dir() or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
            raise ValueError('Runtime and logs must be private operator-owned directories')
    return resolved


def task_view(task):
    if not isinstance(task, dict) or not uuid_value(task.get('id')) or task.get('state') not in STATES:
        raise ValueError('Invalid daemon task')
    result = {'task_id': task['id'], 'state': task['state']}
    for key in ['active_execution_id', 'workspace_id']:
        result[key] = uuid_value(task.get(key))
    result['thread_id'] = uuid_value(task.get('session_id'))
    result['turn_id'] = uuid_value(task.get('turn_id'))
    # No prompt/result/error, config, command, owner, environment or log path is exported.
    enums = {
        'codex_status': {'NOT_STARTED', 'DISPATCHING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN', 'CANCELLED', 'TIMED_OUT', 'LEGACY_UNVERIFIED', 'BLOCKED'},
        'verification_status': {'NOT_VERIFIED', 'RUNNING', 'PASSED', 'FAILED', 'UNKNOWN', 'CANCELLED', 'TIMED_OUT', 'LEGACY_UNVERIFIED', 'BLOCKED'},
        'human_status': {'NOT_REQUIRED', 'PENDING', 'APPROVED', 'REJECTED', 'WAITING'},
        'scheduler_phase': {'RESERVED', 'STARTED', 'AUTHORIZED', 'FINISHED', 'ORPHANED'},
    }
    for key, values in enums.items():
        result[key] = task.get(key) if task.get(key) in values else None
    # Export only fixed diagnostic codes, never the source error or environment.
    if 'BLOCKED' in (result['codex_status'], result['verification_status']):
        try:
            block = json.loads(task.get('error') or '{}')
        except (ValueError, TypeError):
            block = {}
        if isinstance(block, dict) and block.get('reason_code') in (
                'NESTED_ISOLATION_FORBIDDEN', 'ISOLATION_CONTEXT_UNKNOWN'):
            result['blocking_reason'] = block['reason_code']
    for key in ['completion_verified', 'worker_pending']:
        result[key] = task.get(key) is True
    return result


def tool_definitions(writes=False, local_llm=False, local_models=False, chat_mailbox=False, codex_pipeline=False, experiments=False):
    identifier = {'type': 'string', 'pattern': UUID_PATTERN, 'maxLength': 36}
    def tool(name, description, properties, required=(), read=True):
        return {'name': name, 'description': description,
                'inputSchema': {'type': 'object', 'properties': properties,
                                'required': list(required), 'additionalProperties': False},
                'outputSchema': {'type': 'object', 'properties': {'call_id': identifier, 'data': {'type': 'object'}},
                                 'required': ['call_id', 'data'], 'additionalProperties': False},
                'annotations': {'readOnlyHint': read, 'destructiveHint': not read,
                                'idempotentHint': read, 'openWorldHint': False}}
    result = [
        tool('orchestrator_status', 'Read scheduler metadata. Does not execute a task.', {}),
        tool('orchestrator_list_tasks', 'Read a bounded page of task metadata.', {
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
            'offset': {'type': 'integer', 'minimum': 0, 'maximum': 1000000}}),
        tool('orchestrator_get_task', 'Read state and verification metadata. Task text and logs are not exported.',
             {'task_id': identifier}, ['task_id'])]
    if local_llm:
        result.append(tool('aceis_local_llm_generate',
            'Generate an UNVERIFIED local coding suggestion. No code execution or model loading.', {
                'task': {'type': 'string', 'enum': ['draft', 'review']},
                'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 16384},
                'max_tokens': {'type': 'integer', 'minimum': 1, 'maximum': 1024}},
            ['task', 'prompt'], False))
        result[-1]['annotations'].update(destructiveHint=False, idempotentHint=False)
    if local_models:
        profile = {'type': 'string', 'enum': ['qwen-coder-30b', 'gpt-oss-120b', 'qwen-coder-next']}
        result.append(tool('aceis_local_model_status', 'Read fixed model availability. Never load or generate.',
                           {'profile': profile}, ['profile']))
        result.append(tool('aceis_local_model_generate', 'Generate UNVERIFIED advice from an explicitly selected loaded model. No autoload or fallback.',
            {'profile': profile, 'task': {'type': 'string', 'enum': ['draft', 'review']},
             'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 16384},
             'max_tokens': {'type': 'integer', 'minimum': 1, 'maximum': 1024}}, ['profile', 'task', 'prompt'], False))
        result[-1]['annotations'].update(destructiveHint=False, idempotentHint=False)
        result.append(tool('aceis_local_model_prepare', 'Explicitly unload/load one approved profile within the existing router. Temporarily interrupts other profiles; no Docker or commands.',
                           {'profile': profile}, ['profile'], False))
        result[-1]['annotations'].update(destructiveHint=False, idempotentHint=False)
    if chat_mailbox:
        common = {'task_id': identifier, 'request_id': identifier,
                  'request_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}
        required = ['task_id', 'request_id', 'request_sha256']
        body = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
        definitions = [
            (MAILBOX_TOOLS[0], 'Persist one fixed, non-secret question. No Chat/task activation. Same content is idempotent.',
             {**common, 'request': body}, required + ['request'], False),
            (MAILBOX_TOOLS[1], 'Read one allowlisted question and hash. IDs/hashes do not prove Chat identity.', common, required, True),
            (MAILBOX_TOOLS[2], 'Save one non-secret answer bound to the immutable question hash. Reject conflicting content.',
             {**common, 'answer': body, 'answer_sha256': common['request_sha256']}, required + ['answer'], False),
            (MAILBOX_TOOLS[3], 'Read/reconcile persisted answer after missing acknowledgement; never blindly retry UNKNOWN.',
             {**common, 'expected_answer_sha256': common['request_sha256']}, required, True)]
        for name, description, properties, needed, read in definitions:
            result.append(tool(name, description, properties, needed, read))
            result[-1]['annotations'].update(destructiveHint=False, idempotentHint=True)
    if writes:
        result += [
            tool('orchestrator_submit_task', 'Queue one task under the operator-fixed workspace and acceptance policy. Reuse request_key after uncertain delivery.', {
                'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
                'request_key': {'type': 'string', 'pattern': KEY_PATTERN, 'maxLength': 128}}, ['prompt', 'request_key'], False),
            tool('orchestrator_cancel_task', 'Request cancellation of one task. Does not roll back side effects.',
                 {'task_id': identifier}, ['task_id'], False)]
    if codex_pipeline:
        for definition in pipeline_tools():
            definition['outputSchema'] = {'type': 'object', 'properties': {'call_id': identifier, 'data': {'type': 'object'}},
                                         'required': ['call_id', 'data'], 'additionalProperties': False}
            result.append(definition)
    if experiments:
        for definition in experiment_tools():
            definition['outputSchema'] = {'type': 'object', 'properties': {'call_id': identifier, 'data': {'type': 'object'}},
                'required': ['call_id', 'data'], 'additionalProperties': False}
            result.append(definition)
    return result


class Adapter:
    def __init__(self, root, allow_writes=False, allow_local_llm=False, allow_local_models=False, allow_chat_mailbox=False, allow_mailbox_grants=False,
                 allow_codex_pipeline=False, pipeline=None, allow_experiments=False):
        self.root = runtime_path(root)
        if pipeline is not None and not allow_codex_pipeline: raise ValueError('Pipeline activation is explicit')
        if allow_codex_pipeline and pipeline is None: raise ValueError('Pipeline executor must be explicitly connected')
        self.pipeline = pipeline
        self.tools = tool_definitions(allow_writes, allow_local_llm, allow_local_models, allow_chat_mailbox, allow_codex_pipeline, allow_experiments)
        self.experiments = ExperimentStore.from_runtime(self.root) if allow_experiments else None
        self.mailbox = Mailbox(self.root) if allow_chat_mailbox else None
        self.grant = None
        if allow_mailbox_grants:
            if not allow_chat_mailbox:raise ValueError('Mailbox grants require the existing mailbox tools')
            from mailbox_grants import load_runtime_grant,GrantLedger
            policy=load_runtime_grant(self.root)
            if any(task==policy.task_id for task,_ in self.mailbox.allowed):
                raise ValueError('Grant task namespace conflicts with a fixed mailbox task')
            self.grant=GrantLedger(self.root,policy,enabled=True)
        self.helper_root = None
        if allow_local_llm or allow_local_models:
            path = self.root/'local-llm.json'
            info = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096:
                raise ValueError('Local helper configuration must be a private regular file')
            config = decode(path.read_bytes())
            fields(config, ['schema', 'helper_directory'], ['schema', 'helper_directory'])
            if config['schema'] != 1 or not isinstance(config['helper_directory'], str): raise ValueError('Invalid helper configuration')
            base = Path(config['helper_directory'])
            if not base.is_absolute() or any(p.is_symlink() for p in [base, *base.parents]) or not base.is_dir() or base.stat().st_uid != os.getuid():
                raise ValueError('Unsafe helper directory')
            self.helper_root = base
        self.policy = None
        if allow_writes:
            path = self.root / 'mcp-task-policy.json'
            s = path.lstat()
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_size > 16384:
                raise ValueError('Task policy must be a private regular file, at most 16 KiB')
            policy = decode(path.read_bytes())
            fields(policy, ['workspace', 'acceptance', 'timeout'], ['workspace', 'acceptance', 'timeout'])
            fields(policy['workspace'], ['fixture', 'access', 'role'])
            workspace = spec(**policy['workspace'])
            if workspace['role'] != 'implementer': raise ValueError('Initial adapter supports implementer policies only')
            normalize_acceptance(policy['acceptance'])  # Same approved command registry; daemon revalidates.
            if type(policy['timeout']) not in [int, float] or not math.isfinite(policy['timeout']) or not 0 < policy['timeout'] <= 3600:
                raise ValueError('Invalid fixed task timeout')
            self.policy = policy
        self.session = str(uuid.uuid4())
        self.audit_path = self.root / 'logs' / ('mcp-' + self.session + '.jsonl')
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.audit = os.fdopen(fd, 'w')
        self.initialized = False
        self.ready = False

    def record(self, event, **facts):
        line = json.dumps({'time': time.time(), 'session_id': self.session, 'event': event, **facts}) + '\n'
        if self.audit.tell() + len(line.encode()) > AUDIT_LIMIT: raise OSError('Audit capacity reached')
        self.audit.write(line); self.audit.flush(); os.fsync(self.audit.fileno())

    def daemon(self, method, params):
        runtime_path(self.root)
        return request(self.root, method, params, timeout=TIMEOUT)

    def mark_local_uncertain(self, call_id):
        if self.helper_root is None: return
        base = self.helper_root/'.runtime'
        try:
            info = base.lstat()
            if base.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700: return
            path = base/'UNCERTAIN.json'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'code': 'MCP_HELPER_ERROR_OR_UNCERTAIN', 'call_id': call_id,
                           'time': time.time(), 'automatic_retry': False}, stream)
        except OSError: pass  # Existing uncertainty is preserved; nothing is deleted.

    def call_tool(self, name, arguments, rpc_id):
        if name not in {t['name'] for t in self.tools}: raise Invalid('Tool is not enabled')
        if name == 'orchestrator_status': fields(arguments, [])
        elif name in EXPERIMENT_TOOLS:
            try: experiment_arguments(name, arguments)
            except EvidenceError as exc: raise Invalid(exc.code) from None
        elif name in PIPELINE_TOOLS:
            try: pipeline_arguments(name, arguments)
            except PipelineError as exc: raise Invalid(exc.code) from None
        elif name in MAILBOX_TOOLS:
            try: mailbox_arguments(name, arguments)
            except MailboxError as exc: raise Invalid(exc.code) from None
        elif name == 'orchestrator_list_tasks':
            fields(arguments, ['limit', 'offset'])
            for k, default, low, high in [('limit', 20, 1, 50), ('offset', 0, 0, 1000000)]:
                v = arguments.get(k, default)
                if type(v) is not int or not low <= v <= high: raise Invalid('Invalid list bound')
        elif name in ['aceis_local_model_status', 'aceis_local_model_generate', 'aceis_local_model_prepare']:
            allowed = ['profile', 'task', 'prompt', 'max_tokens'] if name == 'aceis_local_model_generate' else ['profile']
            required = ['profile', 'task', 'prompt'] if name == 'aceis_local_model_generate' else ['profile']
            fields(arguments, allowed, required)
            if not isinstance(arguments['profile'], str) or arguments['profile'] not in ['qwen-coder-30b', 'gpt-oss-120b', 'qwen-coder-next']:
                raise Invalid('Invalid selected profile')
            if name == 'aceis_local_model_generate':
                if arguments['task'] not in ['draft', 'review']: raise Invalid('Invalid selected task')
                prompt, tokens = arguments['prompt'], arguments.get('max_tokens', 256)
                if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 16384: raise Invalid('Invalid selected prompt')
                if type(tokens) is not int or not 1 <= tokens <= 1024: raise Invalid('Invalid selected output bound')
        elif name == 'aceis_local_llm_generate':
            fields(arguments, ['task', 'prompt', 'max_tokens'], ['task', 'prompt'])
            if arguments['task'] not in ['draft', 'review']: raise Invalid('Invalid local task')
            prompt = arguments['prompt']
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 16384:
                raise Invalid('Invalid local prompt bound')
            tokens = arguments.get('max_tokens', 256)
            if type(tokens) is not int or not 1 <= tokens <= 1024: raise Invalid('Invalid local output bound')
        elif name in ['orchestrator_get_task', 'orchestrator_cancel_task']:
            fields(arguments, ['task_id'], ['task_id'])
            if not uuid_value(arguments['task_id']): raise Invalid('Invalid task ID')
        else:
            fields(arguments, ['prompt', 'request_key'], ['prompt', 'request_key'])
            prompt, key = arguments['prompt'], arguments['request_key']
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4096 or len(prompt.encode()) > 16384:
                raise Invalid('Invalid prompt length')
            if not isinstance(key, str) or not re.fullmatch(KEY_PATTERN, key): raise Invalid('Invalid request_key')
        call_id = str(uuid.uuid4())
        facts = {'call_id': call_id, 'tool': name,
                 'rpc_id_hash': hashlib.sha256(json.dumps(rpc_id).encode()).hexdigest()}
        if 'task_id' in arguments: facts['task_id'] = arguments['task_id']
        if name in MAILBOX_TOOLS:
            facts['request_id'] = arguments['request_id']
            facts['request_sha256'] = arguments['request_sha256']
        if 'request_key' in arguments: facts['request_key_hash'] = hashlib.sha256(arguments['request_key'].encode()).hexdigest()
        # Reserve space for an end record before any side effect. Never run if audit cannot be persisted.
        if self.audit.tell() > AUDIT_LIMIT - 4096: raise OSError('Audit capacity reached')
        self.record('begin', **facts)
        try:
            if name in EXPERIMENT_TOOLS:
                try:
                    data = self.experiments.dispatch(name, arguments)
                    failed = False
                except (EvidenceError, OSError, TypeError, KeyError, UnicodeError):
                    data, failed = {'status': 'UNVERIFIED', 'code': 'EVIDENCE_UNAVAILABLE_OR_INVALID'}, True
                self.record('end', outcome='error_or_uncertain' if failed else 'returned', **facts)
                content = {'call_id': call_id, 'data': data}
                return {'content': [{'type': 'text', 'text': json.dumps(content)}],
                        'structuredContent': content, 'isError': failed}
            if name in PIPELINE_TOOLS:
                try:
                    data = self.pipeline.dispatch(name, arguments)
                    failed = data.get('status') in ('UNKNOWN', 'FAILED', 'BLOCKED') or data.get('state') in ('UNKNOWN', 'BLOCKED')
                except PipelineError as exc:
                    data, failed = exc.result(), True
                self.record('end', outcome='error_or_uncertain' if failed else 'returned', **facts)
                content = {'call_id': call_id, 'data': data}
                return {'content': [{'type': 'text', 'text': json.dumps(content)}],
                        'structuredContent': content, 'isError': failed}
            if name in MAILBOX_TOOLS:
                try:
                    backend=(self.grant if self.grant is not None and arguments['task_id']==self.grant.policy.task_id else self.mailbox)
                    data = backend.dispatch(name, arguments)
                    failed = False
                except MailboxError as exc:
                    data, failed = exc.result(), True
                metadata={k:data[k] for k in ['status','code','answer_sha256','duplicate','grant_id','grant_state'] if k in data}
                self.record('end', outcome='error_or_uncertain' if failed else 'returned',
                            mailbox=metadata, **facts)
                content = {'call_id': call_id, 'data': data}
                return {'content': [{'type': 'text', 'text': json.dumps(content)}],
                        'structuredContent': content, 'isError': failed}
            if name in ['aceis_local_llm_generate', 'aceis_local_model_status', 'aceis_local_model_generate', 'aceis_local_model_prepare']:
                # Operator-fixed directory; caller cannot supply paths or commands.
                selected = name != 'aceis_local_llm_generate'
                helper = self.helper_root/('selection_extension.py' if selected else 'extension.py')
                bound = 90
                if name == 'aceis_local_model_status': bound = 15
                elif name == 'aceis_local_model_prepare': bound = 130
                elif name == 'aceis_local_model_generate':
                    bound = {'qwen-coder-30b': 90, 'gpt-oss-120b': 250, 'qwen-coder-next': 190}[arguments['profile']]
                info = helper.lstat()
                if helper.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError('Unsafe local helper')
                completed = subprocess.run(['/usr/bin/python3', '-B', str(helper), '--call'],
                    input=json.dumps({'name': name, 'arguments': arguments}, ensure_ascii=False),
                    capture_output=True, text=True, timeout=bound,
                    env={'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'C.UTF-8'})
                if len(completed.stdout.encode()) > 2*1024**2: raise ValueError('Local result bound exceeded')
                result = json.loads(completed.stdout)
                if type(result.get('isError')) is not bool or not isinstance(result.get('structuredContent'), dict):
                    raise ValueError('Invalid local envelope')
                if completed.returncode != (1 if result['isError'] else 0): raise ValueError('Local exit mismatch')
                data = result['structuredContent']
                if name != 'aceis_local_model_status' and data.get('execution_status') == 'UNKNOWN': self.mark_local_uncertain(call_id)
                data.update(verification_status='NOT_EXECUTED', code_executed=False)
                if name in ['aceis_local_llm_generate', 'aceis_local_model_generate']: data['untrusted_advisory'] = True
                self.record('end', outcome='error_or_uncertain' if result['isError'] else 'returned', **facts)
                content = {'call_id': call_id, 'data': data}
                return {'content': [{'type': 'text', 'text': json.dumps(content)}],
                        'structuredContent': content, 'isError': result['isError']}
            if name == 'orchestrator_status':
                raw = self.daemon('status', {})
                data = {'paused': raw.get('paused') is True, 'stopping': raw.get('stopping') is True,
                        'maximum_sessions': 2, 'blocked_count': len(raw.get('blocked', [])),
                        'pending_recovery_count': len(raw.get('pending_recovery', [])),
                        'active_task_ids': [v for v in raw.get('active_tasks', []) if uuid_value(v)],
                        'task_counts': {k: v for k, v in raw.get('task_counts', {}).items()
                                        if k in STATES and type(v) is int and 0 <= v <= 1000000000}}
            elif name == 'orchestrator_list_tasks':
                raw = self.daemon('list', {'limit': arguments.get('limit', 20), 'offset': arguments.get('offset', 0)})
                if not isinstance(raw, list) or len(raw) > arguments.get('limit', 20): raise ValueError('Invalid list')
                data = {'tasks': [task_view(t) for t in raw]}
            elif name == 'orchestrator_submit_task':
                raw = self.daemon('submit', {**arguments, **self.policy})
                if not uuid_value(raw.get('task_id')): raise ValueError('Invalid submit result')
                data = {'task_id': raw['task_id'], 'accepted': True}
            else:
                raw = self.daemon('cancel' if name == 'orchestrator_cancel_task' else 'status', arguments)
                data = task_view(raw)
                if data['task_id'] != arguments['task_id']: raise ValueError('Unexpected task ID')
            if 'task_id' in data: facts['task_id'] = data['task_id']
            if data.get('active_execution_id'): facts['execution_id'] = data['active_execution_id']
            self.record('end', outcome='returned', **facts)
            content = {'call_id': call_id, 'data': data}
            return {'content': [{'type': 'text', 'text': json.dumps(content)}], 'structuredContent': content, 'isError': False}
        except (OSError, EOFError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError):
            if name.startswith('aceis_local_') and name != 'aceis_local_model_status': self.mark_local_uncertain(call_id)
            self.record('end', outcome='error_or_uncertain', **facts)
            # No daemon exception text, task content or local configuration crosses this boundary.
            body = {'call_id': call_id, 'code': 'LOCAL_LLM_ERROR_OR_UNCERTAIN' if name.startswith('aceis_local_') else 'DAEMON_ERROR_OR_UNCERTAIN',
                    'retry_same_request_key': name == 'orchestrator_submit_task'}
            return {'content': [{'type': 'text', 'text': json.dumps(body)}], 'isError': True}

    def handle(self, message):
        # Discovery failures need evidence too. Never record params, client metadata,
        # raw IDs or unknown method strings; tool calls retain their existing audit.
        method = message.get('method') if isinstance(message, dict) else None
        observed = isinstance(message, dict) and 'id' in message and method in ('initialize', 'server/discover', 'tools/list')
        try:
            if observed: self.record('protocol_request', method=method)
            reply = self._handle(message)
            if observed:
                code = reply.get('error', {}).get('code') if reply else None
                facts = {'method': method, 'error_code': code}
                if method == 'tools/list' and code is None and reply:
                    tools = reply.get('result', {}).get('tools')
                    if isinstance(tools, list):
                        # Fingerprint the actual response, not configured flags.
                        # No caller metadata, raw request IDs, or tool payloads.
                        canonical = json.dumps(tools, sort_keys=True, separators=(',', ':'),
                                               ensure_ascii=False, allow_nan=False).encode()
                        facts.update(tool_count=len(tools),
                                     tools_schema_sha256=hashlib.sha256(canonical).hexdigest())
                self.record('protocol_response', **facts)
            return reply
        except OSError:
            identifier = message.get('id') if isinstance(message, dict) else None
            if not (type(identifier) is int or isinstance(identifier, str) and len(identifier) <= 128): identifier = None
            return self.error(identifier, -32603, 'Local adapter audit unavailable')

    def _handle(self, message):
        identifier = message.get('id') if isinstance(message, dict) else None
        if (not isinstance(message, dict) or message.get('jsonrpc') != '2.0'
                or not isinstance(message.get('method'), str)
                or set(message) - {'jsonrpc', 'id', 'method', 'params'}):
            return self.error(None, -32600, 'Invalid JSON-RPC request')
        if 'id' in message and not (type(identifier) is int or isinstance(identifier, str) and len(identifier) <= 128):
            return self.error(None, -32600, 'Invalid JSON-RPC request ID')
        try:
            fields(message, ['jsonrpc', 'id', 'method', 'params'], ['jsonrpc', 'method'])
            if message['jsonrpc'] != '2.0' or not isinstance(message['method'], str): raise Invalid('Invalid envelope')
            if 'id' in message and not ((type(identifier) is int) or (isinstance(identifier, str) and len(identifier) <= 128)):
                raise Invalid('Invalid request ID')
            params = message.get('params', {})
            if not isinstance(params, dict): raise Invalid('params must be an object')
            method = message['method']
            if 'id' not in message:
                if method == 'notifications/initialized' and self.initialized:
                    fields(params, ['_meta']); self.ready = True
                return None
            meta = params.get('_meta', {})
            if not isinstance(meta, dict): raise Invalid('Invalid metadata')
            modern = method != 'initialize' and (method == 'server/discover' or VERSION_META in meta or CAPABILITIES_META in meta)
            if modern:
                version = meta.get(VERSION_META)
                if (not isinstance(version, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', version)
                        or not isinstance(meta.get(CAPABILITIES_META), dict)):
                    raise Invalid('Missing per-request protocol metadata')
                if version != REQUEST_PROTOCOL:
                    reply = self.error(identifier, -32022, 'Unsupported protocol version')
                    reply['error']['data'] = {'supported': [REQUEST_PROTOCOL], 'requested': version}
                    return reply
                if CLIENT_META in meta:
                    info = meta[CLIENT_META]
                    if not isinstance(info, dict) or any(not isinstance(info.get(k), str) or len(info[k]) > 128 for k in ['name', 'version']):
                        raise Invalid('Invalid clientInfo')
            if method == 'initialize':
                if self.initialized: raise Invalid('Already initialized')
                fields(params, ['protocolVersion', 'capabilities', 'clientInfo', '_meta'], ['protocolVersion', 'capabilities', 'clientInfo'])
                if not isinstance(params['protocolVersion'], str) or not isinstance(params['capabilities'], dict): raise Invalid('Invalid negotiation')
                info = params['clientInfo']
                if not isinstance(info, dict) or any(not isinstance(info.get(k), str) or len(info[k]) > 128 for k in ['name', 'version']):
                    raise Invalid('Invalid clientInfo')
                self.initialized = True
                result = {'protocolVersion': PROTOCOL, 'capabilities': {'tools': {'listChanged': False}},
                          'serverInfo': dict(SERVER_INFO)}
            elif method == 'server/discover':
                fields(params, ['_meta'])
                result = {'supportedVersions': [REQUEST_PROTOCOL, PROTOCOL],
                          'capabilities': {'tools': {'listChanged': False}}, 'ttlMs': 0, 'cacheScope': 'private'}
            elif method == 'ping': result = {}
            elif not modern and not self.ready: raise Invalid('Initialization must complete first')
            elif method == 'tools/list':
                fields(params, ['_meta', 'cursor'])
                # The bounded tool set fits one page; no continuation cursor exists.
                if params.get('cursor') is not None: raise Invalid('Unknown cursor')
                result = {'tools': self.tools}
                if modern: result.update(ttlMs=0, cacheScope='private')
            elif method == 'tools/call':
                fields(params, ['name', 'arguments', '_meta'], ['name'])
                result = self.call_tool(params['name'], params.get('arguments', {}), identifier)
            else: return self.error(identifier, -32601, 'Method not found')
            if modern:
                # Modern metadata belongs to this request only, never to the legacy
                # connection state. Client capabilities cannot enable local writes.
                result = {**result, 'resultType': 'complete', '_meta': {SERVER_META: dict(SERVER_INFO)}}
            return {'jsonrpc': '2.0', 'id': identifier, 'result': result}
        except (Invalid, TypeError):
            if isinstance(message,dict) and message.get('method') == 'tools/call':
                params = message.get('params',{}) if isinstance(message,dict) else {}
                name = params.get('name') if isinstance(params,dict) else None
                enabled={t['name'] for t in self.tools}
                self.record('tool_rejected',tool=name if isinstance(name,str) and name in enabled else 'unknown_or_disabled',
                            error_code=-32602,rpc_id_hash=hashlib.sha256(json.dumps(identifier).encode()).hexdigest())
            if isinstance(message, dict) and 'id' not in message: return None
            valid_id = identifier if type(identifier) is int or isinstance(identifier, str) and len(identifier) <= 128 else None
            return self.error(valid_id, -32602, 'Invalid request parameters')
        except (OSError, ValueError, RuntimeError):
            return self.error(identifier, -32603, 'Local adapter unavailable; do not blindly repeat writes')

    @staticmethod
    def error(identifier, code, text):
        return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': code, 'message': text}}


def serve(adapter):
    selector = selectors.DefaultSelector()
    os.set_blocking(sys.stdin.fileno(), False)
    os.set_blocking(sys.stdout.fileno(), False)
    selector.register(sys.stdin, selectors.EVENT_READ)
    buffer = bytearray()
    partial_since = None
    def emit(message):
        raw = json.dumps(message, ensure_ascii=False, allow_nan=False).encode() + b'\n'
        if len(raw) > MAX_OUTPUT: raw = json.dumps(adapter.error(message.get('id'), -32603, 'Result exceeds output bound')).encode() + b'\n'
        deadline = time.monotonic() + TIMEOUT
        while raw:
            try: count = os.write(sys.stdout.fileno(), raw); raw = raw[count:]
            except BlockingIOError:
                if time.monotonic() >= deadline: raise TimeoutError('Output deadline exceeded')
                time.sleep(.01)
    try:
        while True:
            if partial_since is not None and time.monotonic() - partial_since >= 3:
                emit(adapter.error(None, -32700, 'Incomplete input deadline exceeded')); return 1
            if not selector.select(.1): continue
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                if buffer: emit(adapter.error(None, -32700, 'Incomplete JSON frame'))
                return 0 if not buffer else 1
            if not buffer: partial_since = time.monotonic()
            buffer.extend(chunk)
            while b'\n' in buffer:
                line, _, rest = buffer.partition(b'\n'); buffer = bytearray(rest)
                partial_since = time.monotonic() if buffer else None
                if len(line) > MAX_INPUT:
                    emit(adapter.error(None, -32700, 'Input exceeds bound')); return 1
                try: message = decode(line)
                except (ValueError, UnicodeError, RecursionError):
                    emit(adapter.error(None, -32700, 'Invalid JSON')); continue
                if not isinstance(message, dict):
                    emit(adapter.error(None, -32600, 'Invalid JSON-RPC request')); continue
                reply = adapter.handle(message)
                if reply is not None: emit(reply)
                partial_since = time.monotonic() if buffer else None
            if len(buffer) > MAX_INPUT:
                emit(adapter.error(None, -32700, 'Input exceeds bound')); return 1
    except (OSError, TimeoutError): return 1
    finally:
        selector.close(); adapter.audit.close()


def main():
    parser = argparse.ArgumentParser(description='Local MCP stdio adapter; read-only tools by default')
    parser.add_argument('--runtime', type=Path, required=True, help='Operator-fixed existing private runtime')
    parser.add_argument('--allow-writes', action='store_true', help='Local tests only; requires runtime/mcp-task-policy.json')
    parser.add_argument('--allow-local-llm', action='store_true',
                        help='Separately approved bounded localhost inference; does not enable submit/cancel')
    parser.add_argument('--allow-local-models', action='store_true',
                        help='Approved fixed-profile HTTP selection/lifecycle; never Docker or commands')
    parser.add_argument('--allow-chat-mailbox', action='store_true',
                        help='Separately approved fixed-slot non-secret mailbox; default off')
    parser.add_argument('--allow-mailbox-grants',action='store_true',
                        help='Explicit finite workstream grant; requires mailbox and private grant policy, default off')
    parser.add_argument('--allow-codex-pipeline', action='store_true',
                        help='Separately approved app-server executor; requires private fixed policy, default off')
    parser.add_argument('--allow-experiments', action='store_true',
                        help='Read-only pinned experiment projections; requires private experiment-read-policy.json, default off')
    args = parser.parse_args()
    os.umask(0o077)
    pipeline = None
    try:
        if args.allow_codex_pipeline:
            from codex_host import DaemonPipelineProxy
            pipeline = DaemonPipelineProxy(runtime_path(args.runtime))
        return serve(Adapter(args.runtime, args.allow_writes, args.allow_local_llm, args.allow_local_models,
                             args.allow_chat_mailbox, args.allow_mailbox_grants, args.allow_codex_pipeline, pipeline,
                             allow_experiments=args.allow_experiments))
    except (OSError, ValueError, TypeError, sqlite3.Error):
        print('Adapter startup refused: verify private runtime and operator policy.', file=sys.stderr)
        return 1
    finally:
        pass  # The daemon owns executor lifetime, independently of MCP reconnects.


if __name__ == '__main__': raise SystemExit(main())
