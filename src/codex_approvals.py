"""Bounded approval projections. Request content is untrusted, never authority.

No credentials, trust flags or human receipts are accepted from MCP arguments.
Every execution decision needs an independently configured human decision gate.
"""
import hashlib
import json
import re
import uuid
import unicodedata

COMMAND = 'item/commandExecution/requestApproval'
FILE = 'item/fileChange/requestApproval'
PERMISSIONS = 'item/permissions/requestApproval'
VISIBLE_METHODS = frozenset((COMMAND, FILE, PERMISSIONS,
    'item/tool/requestUserInput', 'mcpServer/elicitation/request'))
RESPONDABLE_METHODS = frozenset((COMMAND, FILE))
MAX_APPROVALS_PER_TASK = 32


def fingerprint(message):
    return hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def request_key(value):
    if type(value) is str and 0 < len(value.encode()) <= 160:
        return ('s', value)
    if type(value) is int and -(2**63) <= value < 2**63:
        return ('i', value)
    return None


def preview(value):
    """Conservative omission, not a claim that arbitrary strings are secret-free."""
    if not isinstance(value, str): return None
    # Do not persist obvious secrets/credential material or control characters.
    sensitive = (r'(?i)(token|password|passwd|secret|authorization|api[_-]?key|'
                 r'private[_-]?key|credential|cookie|bearer|gh[pousr]_[a-z0-9]|'
                 r'sk-[a-z0-9]|-----BEGIN|https?://[^\s/]+:[^\s/]+@)')
    if re.search(sensitive, value) or any(unicodedata.category(c) in
            ('Cc', 'Cf', 'Cs', 'Zl', 'Zp') for c in value):
        return '[withheld: sensitive or control content]'
    data = value.encode()
    if len(data) > 1024: return '[withheld: oversized content]'
    return value


def accept_reviewable(message):
    p = message.get('params', {})
    if not isinstance(p, dict) or message.get('method') != COMMAND: return False
    choices = p.get('availableDecisions', ['accept', 'decline'])
    if not isinstance(choices, list) or 'accept' not in choices:
        return False
    command, cwd = p.get('command'), p.get('cwd')
    environment = p.get('environmentId')
    if environment is not None and (not isinstance(environment, str) or not environment
                                     or preview(environment) != environment): return False
    return (p.get('kind', 'command') == 'command' and isinstance(command, str) and bool(command.strip())
            and preview(command) == command and isinstance(cwd, str) and bool(cwd)
            and preview(cwd) == cwd and p.get('additionalPermissions') is None
            and p.get('networkApprovalContext') is None)


def projection(message, *, task_id, thread_id, received_at):
    """Keep the binding hash and bounded display fields, never raw params."""
    if (type(message) is not dict or message.get('method') not in VISIBLE_METHODS
            or request_key(message.get('id')) is None or type(message.get('params')) is not dict):
        return None
    p, method = message['params'], message['method']
    if any(not isinstance(p.get(k), str) or not p[k] or len(p[k].encode()) > 160
           for k in ('threadId', 'turnId')):
        return None
    action = {COMMAND: 'command_execution', FILE: 'file_change',
              PERMISSIONS: 'permission_change'}.get(method, 'human_input')
    target = {}
    for wire, field in [('cwd', 'cwd'), ('grantRoot', 'grant_root'), ('environmentId', 'environment_id')]:
        if p.get(wire) is not None: target[field] = preview(p[wire])
    network = p.get('networkApprovalContext')
    if isinstance(network, dict):
        target['network'] = {k: preview(network[k]) for k in ('host', 'protocol')
                             if isinstance(network.get(k), str)}
    if not target: target['detail_status'] = 'NOT_PROVIDED_BY_REQUEST'
    if method == 'item/tool/requestUserInput':
        questions = p.get('questions', [])
        if isinstance(questions, list):
            target['questions'] = [{'id': preview(q.get('id')), 'question': preview(q.get('question')),
                'options': [{'label': preview(o.get('label')), 'description': preview(o.get('description'))}
                    for o in q.get('options', [])[:8] if isinstance(o, dict)]}
                for q in questions[:4] if isinstance(q, dict) and isinstance(q.get('options', []), list)]
    if method == 'mcpServer/elicitation/request':
        target['server_name'] = preview(p.get('serverName'))
        target['message'] = preview(p.get('message'))
    choices = p.get('availableDecisions', ['accept', 'decline'])
    offered = [d for d in ('accept', 'decline') if isinstance(choices, list) and d in choices]
    return {'id': str(uuid.uuid4()), 'task_id': task_id, 'thread_id': thread_id,
            'native_thread_id': p['threadId'], 'native_turn_id': p['turnId'],
            'item_id': preview(p.get('itemId')), 'callback_id': preview(p.get('approvalId')),
            'action_kind': preview(p.get('kind', 'command' if method == COMMAND else None)),
            'native_request_id': message['id'], 'request_sha256': fingerprint(message),
            'method': method, 'action': action, 'target': target,
            'command_preview': preview(p.get('command')), 'reason_preview': preview(p.get('reason')),
            'risk': 'CRITICAL' if method == PERMISSIONS else 'HUMAN_REVIEW_REQUIRED',
            'risk_basis': 'Conservative classification; arbitrary commands are not proven safe.',
            'human_final_approval_required': True, 'content_untrusted': True,
            'offered_decisions': offered,
            'response_supported': method in RESPONDABLE_METHODS and bool(offered),
            'accept_supported': accept_reviewable(message),
            'status': 'PENDING', 'received_at': received_at}


def public_record(record):
    # RPC IDs are scoped to a live connection, not bearer capabilities. The local
    # approval UUID + immutable hash must be used; never expose a sendable envelope.
    return {k: v for k, v in record.items() if k != 'native_request_id'}
