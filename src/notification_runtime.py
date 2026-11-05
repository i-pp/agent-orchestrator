"""Fail-closed composition for the MCP Events adapter and daemon worker.

Production secret storage and outbound policy are deliberately not selected
here. Callers must provide reviewed providers and explicitly enable both
callback and worker delivery. Tests inject disposable test-only providers.
"""
import os
import hmac
from pathlib import Path
import sqlite3
import stat

from local_principal import OAuthPrincipal
from mcp_events_service import McpEventsService
from notification_outbox import (BoundedResolver, NotificationError,
                                 NotificationOutbox, NotificationWorker)


class NotificationRuntime:
    """Construct both sides over one private source ledger and durable outbox."""

    def __init__(self, runtime, *, secret_cipher=None, owner_hash_key=None,
                 resolver=None, sender=None, callback_verifier=None,
                 authorize_task=None, owner_access_check=None,
                 authenticated_principal=None, authenticated_principals=None,
                 allow_external_callbacks=False,
                 allow_external_notifications=False, poll_seconds=1.0):
        self.runtime_root = Path(runtime).expanduser().absolute()
        self.source_path = self.runtime_root / 'codex-pipeline.sqlite3'
        self.outbox_path = self.runtime_root / 'mcp-events.sqlite3'
        self.secret_cipher = secret_cipher
        self.owner_hash_key = owner_hash_key
        self.resolver = resolver
        self.sender = sender
        self.callback_verifier = callback_verifier
        self.poll_seconds = poll_seconds
        self.enabled = False
        self.disabled_reason = 'EXTERNAL_DELIVERY_DISABLED'
        self.outbox = None
        self.events_service = None

        if (type(allow_external_callbacks) is not bool
                or type(allow_external_notifications) is not bool):
            raise NotificationError('INVALID_NOTIFICATION_POLICY')
        if not (allow_external_callbacks and allow_external_notifications):
            return
        # A local UID or operator attestation is not remote caller identity.
        if authenticated_principals is None:
            authenticated_principals = (() if authenticated_principal is None
                                        else (authenticated_principal,))
        elif isinstance(authenticated_principals, OAuthPrincipal):
            authenticated_principals = (authenticated_principals,)
        if (not isinstance(authenticated_principals, (tuple, list, set, frozenset))
                or not authenticated_principals
                or any(not isinstance(item, OAuthPrincipal) for item in authenticated_principals)):
            self.disabled_reason = 'OAUTH_PRINCIPAL_REQUIRED'
            return
        authenticated_principals = tuple(authenticated_principals)
        identity_set = {item.identity for item in authenticated_principals}
        if len(identity_set) != len(authenticated_principals):
            self.disabled_reason = 'OAUTH_PRINCIPAL_CONFIGURATION_INVALID'
            return
        required = {
            'secret_cipher': secret_cipher,
            'owner_hash_key': owner_hash_key,
            'resolver': resolver,
            'sender': sender,
            'callback_verifier': callback_verifier,
            'authorize_task': authorize_task,
            'owner_access_check': owner_access_check,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            self.disabled_reason = 'NOTIFICATION_PROVIDER_UNAVAILABLE'
            return
        if (not callable(getattr(secret_cipher, 'encrypt', None))
                or not callable(getattr(secret_cipher, 'decrypt', None))
                or not all(callable(value) for name, value in required.items()
                           if name not in {'secret_cipher', 'owner_hash_key'})):
            self.disabled_reason = 'NOTIFICATION_PROVIDER_INVALID'
            return
        verifier_owner = getattr(callback_verifier, '__self__', None)
        verifier_resolver = getattr(callback_verifier, 'resolver', None)
        if verifier_resolver is None and verifier_owner is not None:
            verifier_resolver = getattr(verifier_owner, 'resolver', None)
        if (not isinstance(resolver, BoundedResolver) or resolver.interruptible is not True
                or getattr(sender, 'resolver', None) is not resolver
                or verifier_resolver is not resolver):
            self.disabled_reason = 'BOUNDED_RESOLVER_REQUIRED'
            return
        if not isinstance(owner_hash_key, bytes) or len(owner_hash_key) < 32:
            self.disabled_reason = 'OWNER_HASH_KEY_UNAVAILABLE'
            return
        if not self._private_runtime_ready():
            self.disabled_reason = 'PRIVATE_PIPELINE_LEDGER_UNAVAILABLE'
            return

        try:
            outbox = NotificationOutbox(
                self.outbox_path, source_db_path=self.source_path,
                secret_cipher=secret_cipher, owner_hash_key=owner_hash_key,
                resolver=resolver)
            service = McpEventsService(
                outbox, authorize_task=authorize_task,
                owner_access_check=owner_access_check,
                callback_verifier=callback_verifier,
                allow_external_callbacks=True)
        except (NotificationError, OSError, ValueError, TypeError, sqlite3.Error):
            self.disabled_reason = 'NOTIFICATION_STORAGE_UNAVAILABLE'
            return
        self.outbox = outbox
        self.events_service = service
        self.authenticated_principals = authenticated_principals
        self.enabled = True
        self.disabled_reason = None

    def _private_runtime_ready(self):
        root = self.runtime_root
        if any(path.is_symlink() for path in [root, *root.parents]):
            return False
        try:
            info = root.stat()
            source = self.source_path.lstat()
        except OSError:
            return False
        return (root.is_dir() and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o700
                and stat.S_ISREG(source.st_mode) and source.st_uid == os.getuid()
                and stat.S_IMODE(source.st_mode) == 0o600)

    def adapter_options(self):
        """Return only supported Adapter kwargs; disabled composition exports none."""
        if not self.enabled:
            return {}
        return {'events_service': self.events_service}

    def worker_factory(self, pipeline):
        """Build a worker only when it shares the pipeline ledger and is enabled."""
        if not self.enabled:
            raise NotificationError(self.disabled_reason or 'EXTERNAL_DELIVERY_DISABLED')
        database = getattr(pipeline, 'db', None)
        if database is None or Path(database).absolute() != self.source_path:
            raise NotificationError('PIPELINE_LEDGER_MISMATCH')
        return NotificationWorker(
            self.outbox, self.sender,
            access_check=self.events_service.worker_access_check,
            poll_seconds=self.poll_seconds)


EVENTS_ENABLE_ENV = 'ACEIS_MCP_EVENTS_ENABLED'


def build_systemd_notification_runtime(runtime, *, events_transport=False,
                                       authenticated_principal=None,
                                       authenticated_principals=None,
                                       environ=None, provider_loader=None):
    """Opt into OS-backed providers only after explicit transport and scope gates.

    The default path does not import provider code, read credentials, create an
    outbox, or attempt network access. External delivery requires an exact
    OAuth principal derived from a verified bearer token; `_meta` and local UID
    values are not used as authorization evidence.
    """
    if type(events_transport) is not bool:
        raise NotificationError('INVALID_NOTIFICATION_POLICY')
    if not events_transport:
        return NotificationRuntime(runtime)

    environment = os.environ if environ is None else environ
    enabled = environment.get(EVENTS_ENABLE_ENV, '0')
    if enabled not in ('0', '1'):
        disabled = NotificationRuntime(runtime)
        disabled.disabled_reason = 'INVALID_NOTIFICATION_POLICY'
        return disabled
    if enabled != '1':
        return NotificationRuntime(runtime)
    if authenticated_principals is None and authenticated_principal is not None:
        authenticated_principals = (authenticated_principal,)
    if isinstance(authenticated_principals, OAuthPrincipal):
        authenticated_principals = (authenticated_principals,)
    if (not isinstance(authenticated_principals, (tuple, list, set, frozenset))
            or not authenticated_principals
            or any(not isinstance(item, OAuthPrincipal) for item in authenticated_principals)):
        return NotificationRuntime(
            runtime, allow_external_callbacks=True,
            allow_external_notifications=True,
            authenticated_principals=authenticated_principals)
    authenticated_principals = tuple(authenticated_principals)
    allowed_identities = {item.identity for item in authenticated_principals}
    if len(allowed_identities) != len(authenticated_principals):
        disabled = NotificationRuntime(runtime)
        disabled.disabled_reason = 'OAUTH_PRINCIPAL_CONFIGURATION_INVALID'
        return disabled

    # Do not load systemd credentials unless both explicit flags are true.
    try:
        if provider_loader is None:
            from os_providers import load_systemd_notification_providers
            provider_loader = load_systemd_notification_providers
        providers = provider_loader(environ=environment)
        required_provider_names = {
            'secret_cipher', 'owner_hash_key', 'resolver', 'sender',
            'callback_verifier'}
        if (not isinstance(providers, dict)
                or set(providers) != required_provider_names):
            raise NotificationError('NOTIFICATION_PROVIDER_INVALID')
    except NotificationError as exc:
        disabled = NotificationRuntime(
            runtime, allow_external_callbacks=True,
            allow_external_notifications=True,
            authenticated_principals=authenticated_principals)
        disabled.disabled_reason = exc.code
        return disabled
    except Exception:
        disabled = NotificationRuntime(
            runtime, allow_external_callbacks=True,
            allow_external_notifications=True,
            authenticated_principals=authenticated_principals)
        disabled.disabled_reason = 'NOTIFICATION_PROVIDER_UNAVAILABLE'
        return disabled

    owner_runtime = {}

    def authorize_task(identity, task_id):
        components = owner_runtime.get('runtime')
        if (components is None or components.outbox is None
                or identity not in allowed_identities):
            return False
        return components.outbox.source_task_exists(task_id)

    def owner_access_check(owner_hash, task_id):
        components = owner_runtime.get('runtime')
        if components is None or components.outbox is None:
            raise NotificationError('TASK_AUTHORIZATION_UNAVAILABLE')
        expected = {components.outbox.owner_hash(principal)
                    for principal in authenticated_principals}
        if not isinstance(owner_hash, str) or not any(
                hmac.compare_digest(owner_hash, candidate) for candidate in expected):
            return False
        return (components.outbox.task_owned_by_hash(task_id, owner_hash)
                and components.outbox.source_task_exists(task_id))

    components = NotificationRuntime(
        runtime, **providers, authorize_task=authorize_task,
        owner_access_check=owner_access_check,
        allow_external_callbacks=True, allow_external_notifications=True,
        authenticated_principals=authenticated_principals)
    owner_runtime['runtime'] = components
    return components


def make_host_gateway_factory(notification_runtime_factory):
    """Adapt the existing Daemon.pipeline_factory seam without enabling defaults."""
    if not callable(notification_runtime_factory):
        raise ValueError('Notification runtime factory required')

    def create(runtime, binary, project, ordinary_busy):
        from codex_host import HostGateway
        components = notification_runtime_factory(runtime)
        if not isinstance(components, NotificationRuntime):
            raise NotificationError('INVALID_NOTIFICATION_RUNTIME')
        return HostGateway(
            runtime, binary, project, ordinary_busy,
            notification_runtime=components)

    return create
