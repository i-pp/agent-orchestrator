"""Opt-in OS-backed providers for local MCP Events.

These providers do not create credentials. The key provider only reads a
pre-provisioned systemd credential, and the resolver runs a fixed trusted
getent executable in a killable child process.
"""
import ipaddress
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time

from notification_outbox import (BoundedResolver, NotificationError,
                                 PinnedWebhookSender)


CREDENTIAL_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
MAX_KEY_BYTES = 32
MAX_RESOLVER_OUTPUT_BYTES = 65_536
MAX_RESOLVER_ADDRESSES = 64
GETENT_EXECUTABLE = '/usr/bin/getent'


def _trusted_system_uid(uid):
    """Trust only an explicit root owner; overflow UID provenance is ambiguous.

    uid_map can show that host UID 0 is unmapped, but stat(2) reporting 65534
    does not prove that the underlying owner was root: any unmapped host UID can
    appear as the namespace overflow UID. Without trusted filesystem provenance,
    accepting 65534 would still trust arbitrary unmapped owners.
    """
    return uid == 0


def _validate_getent_executable():
    executable = Path(GETENT_EXECUTABLE)
    if any(parent.is_symlink() for parent in [executable, *executable.parents]):
        raise NotificationError('UNSAFE_RESOLVER_EXECUTABLE')
    try:
        info = executable.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                or not _trusted_system_uid(info.st_uid) or info.st_uid == os.getuid()
                or os.access(executable, os.W_OK) or not os.access(executable, os.X_OK)):
            raise NotificationError('UNSAFE_RESOLVER_EXECUTABLE')
        for parent in executable.parents:
            parent_info = parent.stat()
            if (not stat.S_ISDIR(parent_info.st_mode)
                    or not _trusted_system_uid(parent_info.st_uid)
                    or parent_info.st_uid == os.getuid()
                    or parent_info.st_mode & 0o022 or os.access(parent, os.W_OK)):
                raise NotificationError('UNSAFE_RESOLVER_EXECUTABLE')
    except OSError:
        raise NotificationError('BOUNDED_RESOLVER_UNAVAILABLE') from None


class SystemdCredentialKeyProvider:
    """Read a fixed 32-byte key from systemd's read-only credential directory."""

    def __init__(self, name, *, environ=None):
        if not isinstance(name, str) or CREDENTIAL_NAME_RE.fullmatch(name) is None:
            raise NotificationError('INVALID_CREDENTIAL_NAME')
        environment = os.environ if environ is None else environ
        directory = environment.get('CREDENTIALS_DIRECTORY')
        if not isinstance(directory, str) or not directory:
            raise NotificationError('SYSTEMD_CREDENTIAL_UNAVAILABLE')
        path = Path(directory)
        if (not path.is_absolute() or '..' in path.parts
                or any(parent.is_symlink() for parent in [path, *path.parents])):
            raise NotificationError('UNSAFE_CREDENTIAL_DIRECTORY')
        self.directory = path
        self.name = name

    def get_key(self):
        flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        flags |= getattr(os, 'O_CLOEXEC', 0)
        try:
            directory_fd = os.open(self.directory, flags)
        except OSError:
            raise NotificationError('SYSTEMD_CREDENTIAL_UNAVAILABLE') from None
        try:
            directory_stat = os.fstat(directory_fd)
            directory_mode = stat.S_IMODE(directory_stat.st_mode)
            if (not stat.S_ISDIR(directory_stat.st_mode)
                    or directory_stat.st_uid != os.getuid()
                    or directory_mode & 0o077 or directory_mode & 0o222):
                raise NotificationError('UNSAFE_CREDENTIAL_DIRECTORY')
            file_flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                          | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NONBLOCK', 0))
            try:
                credential_fd = os.open(self.name, file_flags, dir_fd=directory_fd)
            except OSError:
                raise NotificationError('SYSTEMD_CREDENTIAL_UNAVAILABLE') from None
            try:
                info = os.fstat(credential_fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o400 or info.st_nlink != 1
                        or info.st_size != MAX_KEY_BYTES):
                    raise NotificationError('UNSAFE_SYSTEMD_CREDENTIAL')
                value = bytearray()
                while len(value) <= MAX_KEY_BYTES:
                    chunk = os.read(credential_fd, MAX_KEY_BYTES + 1 - len(value))
                    if not chunk:
                        break
                    value.extend(chunk)
                if len(value) != MAX_KEY_BYTES:
                    raise NotificationError('INVALID_SYSTEMD_CREDENTIAL')
                return bytes(value)
            finally:
                os.close(credential_fd)
        finally:
            os.close(directory_fd)


class AesGcmSecretCipher:
    """AES-256-GCM cipher using an existing OS-provided key; never provisions it."""

    MAGIC = b'ACEIS-AESGCM-v1\x00'
    AAD = b'aceis-mcp-events-secret:v1'
    NONCE_BYTES = 12
    TAG_BYTES = 16
    MAX_PLAINTEXT_BYTES = 65_536

    def __init__(self, key_provider):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise NotificationError('AESGCM_UNAVAILABLE') from None
        getter = getattr(key_provider, 'get_key', None)
        if not callable(getter):
            raise NotificationError('SECRET_KEY_PROVIDER_UNAVAILABLE')
        try:
            key = getter()
        except NotificationError:
            raise
        except Exception:
            raise NotificationError('SECRET_KEY_UNAVAILABLE') from None
        if not isinstance(key, bytes) or len(key) != MAX_KEY_BYTES:
            raise NotificationError('INVALID_SECRET_KEY')
        self._aead = AESGCM(key)

    def encrypt(self, value):
        if not isinstance(value, bytes) or len(value) > self.MAX_PLAINTEXT_BYTES:
            raise NotificationError('INVALID_SECRET_VALUE')
        nonce = os.urandom(self.NONCE_BYTES)
        return self.MAGIC + nonce + self._aead.encrypt(nonce, value, self.AAD)

    def decrypt(self, value):
        minimum = len(self.MAGIC) + self.NONCE_BYTES + self.TAG_BYTES
        if (not isinstance(value, bytes) or len(value) < minimum
                or len(value) > minimum + self.MAX_PLAINTEXT_BYTES
                or not value.startswith(self.MAGIC)):
            raise NotificationError('INVALID_SECRET_CIPHERTEXT')
        offset = len(self.MAGIC)
        nonce = value[offset:offset + self.NONCE_BYTES]
        ciphertext = value[offset + self.NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, ciphertext, self.AAD)
        except Exception:
            raise NotificationError('INVALID_SECRET_CIPHERTEXT') from None


class GetentResolver:
    """Resolve via NSS in a killable subprocess with a hard communicate deadline."""

    interruptible = True

    def __init__(self, *, process_factory=subprocess.Popen, max_timeout=2.0):
        if not callable(process_factory) or type(max_timeout) not in (int, float) or not 0 < max_timeout <= 10:
            raise NotificationError('INVALID_RESOLVER_CONFIGURATION')
        _validate_getent_executable()
        self.binary = GETENT_EXECUTABLE
        self.process_factory = process_factory
        self.max_timeout = float(max_timeout)

    def resolve_with_timeout(self, host, timeout_seconds):
        if (not isinstance(host, str) or not host.isascii() or not 1 <= len(host) <= 253
                or type(timeout_seconds) not in (int, float) or timeout_seconds <= 0):
            raise NotificationError('INVALID_RESOLVER_REQUEST')
        budget = min(float(timeout_seconds), self.max_timeout)
        deadline = time.monotonic() + budget
        cleanup_grace = min(0.1, budget * 0.1)
        work_deadline = deadline - cleanup_grace
        try:
            process = self.process_factory(
                [self.binary, 'ahosts', host], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True,
                start_new_session=True, env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'})
        except OSError:
            raise NotificationError('BOUNDED_RESOLVER_UNAVAILABLE') from None
        output = bytearray()
        selector = selectors.DefaultSelector()
        try:
            if process.stdout is None:
                raise NotificationError('CALLBACK_DNS_FAILED')
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            eof = False
            while not eof:
                remaining = work_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Callback DNS deadline exceeded')
                if not selector.select(remaining):
                    raise TimeoutError('Callback DNS deadline exceeded')
                chunk = os.read(process.stdout.fileno(),
                                min(8192, MAX_RESOLVER_OUTPUT_BYTES + 1 - len(output)))
                if not chunk:
                    eof = True
                    selector.unregister(process.stdout)
                    break
                output.extend(chunk)
                if len(output) > MAX_RESOLVER_OUTPUT_BYTES:
                    raise NotificationError('CALLBACK_DNS_RESULT_TOO_LARGE')
            remaining = work_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Callback DNS deadline exceeded')
            return_code = process.wait(timeout=remaining)
            if return_code != 0:
                raise NotificationError('CALLBACK_DNS_FAILED')
        except (TimeoutError, subprocess.TimeoutExpired):
            self._kill_process_group(
                process, wait_timeout=max(0.0, deadline - time.monotonic()))
            raise TimeoutError('Callback DNS deadline exceeded') from None
        except NotificationError as error:
            if error.code == 'CALLBACK_DNS_RESULT_TOO_LARGE':
                self._kill_process_group(
                    process, wait_timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            raise
        except OSError:
            self._kill_process_group(
                process, wait_timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            raise NotificationError('CALLBACK_DNS_FAILED') from None
        finally:
            selector.close()
            if process.stdout is not None:
                process.stdout.close()
        if not output:
            raise NotificationError('CALLBACK_DNS_FAILED')
        addresses = set()
        try:
            for line in bytes(output).decode('ascii').splitlines():
                fields = line.split()
                if not fields:
                    continue
                addresses.add(str(ipaddress.ip_address(fields[0])))
                if len(addresses) > MAX_RESOLVER_ADDRESSES:
                    raise NotificationError('CALLBACK_DNS_RESULT_TOO_LARGE')
        except (UnicodeError, ValueError):
            raise NotificationError('CALLBACK_DNS_FAILED') from None
        if not addresses:
            raise NotificationError('CALLBACK_DNS_FAILED')
        return sorted(addresses)

    @staticmethod
    def _kill_process_group(process, *, wait_timeout):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        if wait_timeout <= 0:
            process.poll()
            return
        try:
            process.wait(timeout=wait_timeout)
        except (subprocess.TimeoutExpired, OSError):
            process.poll()


def load_systemd_notification_providers(*, environ=None,
                                        process_factory=subprocess.Popen):
    """Build providers from pre-provisioned credentials without creating any.

    The returned callbacks are not enabled or contacted by this helper. A
    caller must still provide authoritative task authorization and owner ACLs.
    """
    encryption_key = SystemdCredentialKeyProvider(
        'mcp-events-aes-key', environ=environ).get_key()
    owner_key = SystemdCredentialKeyProvider(
        'mcp-events-owner-hmac-key', environ=environ).get_key()

    class LoadedKey:
        def get_key(self):
            return encryption_key

    cipher = AesGcmSecretCipher(LoadedKey())
    resolver = BoundedResolver(GetentResolver(process_factory=process_factory))
    sender = PinnedWebhookSender(resolver=resolver)
    return {
        'secret_cipher': cipher,
        'owner_hash_key': owner_key,
        'resolver': resolver,
        'sender': sender,
        'callback_verifier': sender.verify_callback,
    }
