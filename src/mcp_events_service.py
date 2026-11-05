"""Transport-neutral MCP Events lifecycle for the offline notification lab.

The current prototype derives its sole owner from the local OS UID. This is
not remote-user authentication: no JSON-RPC identity metadata is accepted,
and external callbacks stay disabled unless explicitly opted in for fixtures.
"""
from notification_outbox import (BLOCKED_REASON_CODES, EVENT_NAMES,
    NotificationError, UUID_RE)
from local_principal import LocalPrincipal, OAuthPrincipal


def _uuid_schema():
    return {'type': 'string', 'format': 'uuid', 'minLength': 36, 'maxLength': 36}


def event_definitions():
    definitions = []
    for event_name, state in sorted((name, state) for name, state in EVENT_NAMES.values()):
        action_required = event_name == EVENT_NAMES['task_blocked'][0]
        data_properties = {
            'task_id': _uuid_schema(),
            'state': {'type': 'string', 'const': state},
            'thread_id': _uuid_schema(),
        }
        data_required = ['task_id', 'state']
        if action_required:
            data_properties.update({
                'action_required': {'type': 'boolean', 'const': True},
                'reason_code': {'type': 'string', 'enum': sorted(BLOCKED_REASON_CODES)},
            })
            data_required.extend(['action_required', 'reason_code'])
        definitions.append({
            'name': event_name,
            'description': ('Notify when an authorized Codex task is blocked and needs user or operator action.'
                            if action_required else
                            f'Notify when an authorized Codex task enters {state}.'),
            'delivery': ['webhook'],
            'inputSchema': {
                'type': 'object',
                'properties': {'task_id': _uuid_schema()},
                'required': ['task_id'],
                'additionalProperties': False,
            },
            'payloadSchema': {
                'type': 'object',
                'properties': {
                    'eventId': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                    'name': {'type': 'string', 'const': event_name},
                    'timestamp': {'type': 'string', 'format': 'date-time'},
                    'data': {
                        'type': 'object',
                        'properties': data_properties,
                        'required': data_required,
                        'additionalProperties': False,
                    },
                    'cursor': {'type': ['string', 'null'], 'maxLength': 32},
                },
                'required': ['eventId', 'name', 'timestamp', 'data', 'cursor'],
                'additionalProperties': False,
            },
        })
    return definitions


class McpEventsService:
    """Implements events/list, events/subscribe and events/unsubscribe.

    `authorize_task(principal, task_id)` must consult the authoritative task
    ACL. `owner_access_check(owner_hash, task_id)` is used again immediately
    before sending each event, so revoked access stops delivery. The callback
    verifier is an injected transport; tests use an in-memory fake.
    """

    METHODS = frozenset({'events/list', 'events/subscribe', 'events/unsubscribe'})

    def __init__(self, outbox, *, authorize_task, owner_access_check,
                 callback_verifier, default_ttl_ms=3_600_000,
                 allow_external_callbacks=False):
        if not callable(authorize_task) or not callable(owner_access_check):
            raise NotificationError('AUTHORIZER_REQUIRED')
        if type(default_ttl_ms) is not int:
            raise NotificationError('INVALID_SUBSCRIPTION_TTL')
        if type(allow_external_callbacks) is not bool:
            raise NotificationError('INVALID_CALLBACK_POLICY')
        self.outbox = outbox
        self.authorize_task = authorize_task
        self.owner_access_check = owner_access_check
        self.callback_verifier = callback_verifier
        self.default_ttl_ms = default_ttl_ms
        self.enabled = allow_external_callbacks

    def _principal(self, principal):
        if not isinstance(principal, (LocalPrincipal, OAuthPrincipal)):
            raise NotificationError('AUTHENTICATED_PRINCIPAL_REQUIRED')
        self.outbox.owner_hash(principal)
        return principal

    def bind_task_owner(self, task_id, principal):
        """Bind only to the authenticated request principal supplied by the transport."""
        principal = self._principal(principal)
        if not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id):
            raise NotificationError('INVALID_TASK_ID')
        try:
            authorized = self.authorize_task(principal.identity, task_id)
        except Exception:
            raise NotificationError('TASK_AUTHORIZATION_UNAVAILABLE') from None
        if authorized is not True:
            raise NotificationError('TASK_NOT_AUTHORIZED')
        return self.outbox.bind_task_origin(task_id, principal)

    def bind_thread_owner(self, thread_id, principal):
        principal = self._principal(principal)
        if not isinstance(thread_id, str) or not UUID_RE.fullmatch(thread_id):
            raise NotificationError('INVALID_THREAD_ID')
        return self.outbox.bind_thread_origin(thread_id, principal)

    def task_owned_by(self, task_id, principal):
        principal = self._principal(principal)
        return self.outbox.task_owned_by(task_id, principal)

    def thread_owned_by(self, thread_id, principal):
        principal = self._principal(principal)
        return self.outbox.thread_owned_by(thread_id, principal)

    def _authorize(self, principal, task_id):
        principal = self._principal(principal)
        if not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id):
            raise NotificationError('INVALID_TASK_ID')
        try:
            authorized = self.authorize_task(principal.identity, task_id)
        except Exception:
            raise NotificationError('TASK_AUTHORIZATION_UNAVAILABLE') from None
        if authorized is not True or not self.outbox.task_owned_by(task_id, principal):
            raise NotificationError('TASK_NOT_AUTHORIZED')
        return principal

    def list_events(self, principal):
        principal = self._principal(principal)
        if not self.enabled:
            return {'events': []}
        for task_id in self.outbox.owned_task_ids(principal):
            try:
                if self.authorize_task(principal.identity, task_id) is True:
                    return {'events': event_definitions()}
            except Exception:
                raise NotificationError('TASK_AUTHORIZATION_UNAVAILABLE') from None
        return {'events': []}

    def subscribe(self, params, principal):
        if not self.enabled:
            raise NotificationError('EXTERNAL_CALLBACKS_DISABLED')
        if not isinstance(params, dict) or set(params) - {
                'name', 'arguments', 'delivery', 'cursor', 'ttlMs'} or not {
                'name', 'arguments', 'delivery'} <= set(params):
            raise NotificationError('INVALID_SUBSCRIBE_PARAMS')
        name = params['name']
        arguments = params['arguments']
        delivery = params['delivery']
        if (not isinstance(delivery, dict) or set(delivery) - {'mode', 'url', 'secret'}
                or set(delivery) != {'mode', 'url', 'secret'}
                or delivery.get('mode') != 'webhook'):
            raise NotificationError('INVALID_DELIVERY_CONFIG')
        if not isinstance(arguments, dict) or set(arguments) != {'task_id'}:
            raise NotificationError('INVALID_SUBSCRIPTION_ARGUMENTS')
        principal = self._principal(principal)
        task_id = arguments['task_id']
        if not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id):
            raise NotificationError('INVALID_TASK_ID')
        # Ownership mapping is durable, but authorization can be revoked later.
        # Recheck the authoritative task ACL before creating or refreshing a hook.
        self._authorize(principal, task_id)
        ttl_ms = params.get('ttlMs', self.default_ttl_ms)
        # The Events API permits a request for a non-expiring subscription.
        # This service does not grant permanent subscriptions: use its finite
        # default lifetime and return that expiration to the caller.
        if ttl_ms is None:
            ttl_ms = self.default_ttl_ms
        try:
            result = self.outbox.subscribe(
                callback_url=delivery['url'], event_name=name, arguments=arguments,
                authenticated_principal=principal, signing_secret=delivery['secret'],
                ttl_ms=ttl_ms, cursor=params.get('cursor'))
        except NotificationError as exc:
            if exc.code in {'INVALID_CALLBACK_URL', 'UNSAFE_CALLBACK_HOST',
                            'CALLBACK_DNS_FAILED', 'UNSAFE_CALLBACK_ADDRESS',
                            'BOUNDED_RESOLVER_REQUIRED'}:
                raise NotificationError('CALLBACK_' + exc.code) from None
            raise
        if result['verification_required']:
            if not callable(self.callback_verifier):
                self.outbox.deactivate_subscription(result['id'], error_code='CALLBACK_VERIFIER_UNAVAILABLE')
                raise NotificationError('CALLBACK_VERIFIER_UNAVAILABLE')
            try:
                verification = self.outbox.build_verification_request(result['id'])
                response = self.callback_verifier(verification)
                status_code = response.get('status_code') if isinstance(response, dict) else None
                echoed = response.get('echoed_challenge') if isinstance(response, dict) else None
                if type(status_code) is not int:
                    status_code, echoed = 0, None
            except TimeoutError:
                self.outbox.complete_verification(
                    result['id'], status_code=0, echoed_challenge=None)
                raise NotificationError('CALLBACK_TIMEOUT') from None
            except Exception:
                status_code, echoed = 0, None
            verified = self.outbox.complete_verification(
                result['id'], status_code=status_code, echoed_challenge=echoed)
            if verified['status'] != 'VERIFIED':
                raise NotificationError('CALLBACK_CHALLENGE_FAILED')
        # MCP response fields only; never return callback IPs or secret material.
        return {key: result[key] for key in ('id', 'refreshBefore', 'cursor', 'truncated')}

    def unsubscribe(self, params, principal):
        if not isinstance(params, dict) or set(params) != {'name', 'arguments', 'delivery'}:
            raise NotificationError('INVALID_UNSUBSCRIBE_PARAMS')
        delivery = params['delivery']
        arguments = params['arguments']
        if (not isinstance(delivery, dict) or set(delivery) != {'mode', 'url'}
                or delivery.get('mode') != 'webhook'
                or not isinstance(arguments, dict) or set(arguments) != {'task_id'}):
            raise NotificationError('INVALID_DELIVERY_CONFIG')
        # Unsubscribe must remain available to the authenticated subscription
        # owner after task access is revoked; current task ACL is not required
        # to stop an existing delivery route.
        principal = self._principal(principal)
        task_id = arguments['task_id']
        if not isinstance(task_id, str) or not UUID_RE.fullmatch(task_id):
            raise NotificationError('INVALID_TASK_ID')
        if not self.outbox.task_owned_by(task_id, principal):
            raise NotificationError('TASK_NOT_AUTHORIZED')
        self.outbox.unsubscribe(callback_url=delivery['url'], event_name=params['name'],
                                arguments=arguments, authenticated_principal=principal)
        return {}

    def handle(self, method, params, principal):
        if method == 'events/list':
            if not isinstance(params, dict) or set(params) - {'cursor'}:
                raise NotificationError('INVALID_LIST_PARAMS')
            if params.get('cursor') is not None:
                raise NotificationError('EVENT_CATALOG_CURSOR_UNSUPPORTED')
            return self.list_events(principal)
        if method == 'events/subscribe':
            return self.subscribe(params, principal)
        if method == 'events/unsubscribe':
            return self.unsubscribe(params, principal)
        raise NotificationError('UNKNOWN_EVENTS_METHOD')

    def worker_access_check(self, owner_hash, task_id):
        """Fail closed when task access cannot be revalidated after restart."""
        try:
            allowed = self.owner_access_check(owner_hash, task_id)
        except Exception:
            raise
        if type(allowed) is not bool:
            raise NotificationError('TASK_AUTHORIZATION_UNAVAILABLE')
        return allowed
