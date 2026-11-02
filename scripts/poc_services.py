"""Manual, detached PoC services. No install/enable, credential reads or task writes."""

import argparse
from contextlib import contextmanager, closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT/'src'))
from control_socket import endpoint
from development_poc import MODE, MARKER
from service_wait import daemon_ready, tunnel_ready

CLIENT = Path.home()/'.local/opt/tunnel-client/v0.0.18/tunnel-client'
RUNTIME = PROJECT/'.runtime/development-poc-e32-c00a7a45'
CODEX = Path.home()/'.vscode/extensions/openai.chatgpt-26.1021.22027-linux-x64/bin/linux-x86_64/codex'
KEY = Path.home()/'.config/agent-orchestrator/credentials/control-plane-api-key'
SUPERVISOR_MEMORY_BYTES = 512 * 1024**2
TUNNEL_MEMORY_BYTES = 256 * 1024**2
SUPERVISOR_TASKS = 64
TUNNEL_TASKS = 64
STATE = 'poc-services-state.json'
ENV = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'C.UTF-8'}
ENV.update(XDG_RUNTIME_DIR='/run/user/'+str(os.getuid()),
           DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/'+str(os.getuid())+'/bus')
UNSET = ('CONTROL_PLANE_API_KEY OPENAI_API_KEY OPENAI_ADMIN_KEY LOG_HTTP_RAW_UNSAFE '
         'MCP_SERVER_URL MCP_COMMAND HARPOON_TARGETS HARPOON_ADDITIONAL_TRANSPORTS '
         'TUNNEL_CLIENT_CONFIG TUNNEL_CLIENT_PROFILE TUNNEL_CLIENT_PROFILE_FILE '
         'TUNNEL_CLIENT_PROFILE_DIR CONTROL_PLANE_POLL_CHANNELS')


class Refused(RuntimeError):
    pass


def private_file(path, maximum=16384):
    """Metadata only, including when path is a runtime credential."""
    path = Path(path).expanduser().absolute()
    for parent in [path, *path.parents]:
        s = parent.lstat()
        if stat.S_ISLNK(s.st_mode): raise Refused('Symlink path refused')
    s = path.lstat()
    if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600
            or not 0 < s.st_size <= maximum or s.st_nlink != 1):
        raise Refused('Expected private operator-owned regular file (0600)')
    return {'path': str(path), 'owner_uid': s.st_uid, 'mode': '0600', 'size': s.st_size}


def credential_metadata(path):
    result = private_file(path)
    for parent in Path(result['path']).parents:
        s = parent.lstat()
        trusted_tmp = s.st_uid == 0 and bool(s.st_mode & stat.S_ISVTX)
        if s.st_uid not in [0, os.getuid()] or (s.st_mode & 0o022 and not trusted_tmp):
            raise Refused('Writable or foreign credential ancestry refused')
    return result


class Services:
    def __init__(self, runtime=RUNTIME, codex=CODEX, key_file=KEY, allow_local_llm=False, allow_local_models=False, allow_chat_mailbox=False, allow_mailbox_grants=False, allow_codex_pipeline=False, allow_experiments=False):
        if allow_mailbox_grants and not allow_chat_mailbox:
            raise Refused('Mailbox grants require the chat mailbox capability')
        if type(allow_codex_pipeline) is not bool:
            raise Refused('Pipeline capability flag must be boolean')
        if type(allow_experiments) is not bool: raise Refused('Experiment flag must be boolean')
        self.root = Path(runtime).expanduser().absolute()
        if (self.root.parent != PROJECT/'.runtime' or not re.fullmatch(r'development-poc-[A-Za-z0-9_-]{1,80}', self.root.name)
                or any(p.is_symlink() for p in [self.root, *self.root.parents])):
            raise Refused('Only a dedicated existing development-poc runtime is allowed')
        for p in [self.root, self.root/'logs']:
            s = p.stat()
            if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700 or p.is_symlink():
                raise Refused('Runtime and logs require private owner directories')
        marker = self.root/MARKER
        private_file(marker, 1024)
        if json.loads(marker.read_text()) != {'schema': 1, 'mode': MODE, 'runtime': str(self.root)}:
            raise Refused('Existing PoC declaration does not match')
        self.codex, self.client, self.key = Path(codex).absolute(), CLIENT, Path(key_file).expanduser().absolute()
        suffix = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        self.units = {role: 'agentorchestrator-poc-'+suffix+'-'+role+'.service' for role in ['daemon', 'tunnel']}
        self.health = self.root/'poc-service-health-url'
        self.allow_local_llm = allow_local_llm
        self.allow_local_models = allow_local_models
        self.allow_chat_mailbox = allow_chat_mailbox
        self.allow_mailbox_grants = allow_mailbox_grants
        self.allow_codex_pipeline = allow_codex_pipeline
        self.allow_experiments = allow_experiments

    def pipeline_snapshot(self, expected=None):
        """Read fixed executor/policy metadata; no executor or auth startup."""
        from codex_host import executor_snapshot
        value = executor_snapshot(self.root, self.codex)
        keys = {'executor_policy_sha256', 'pipeline_policy_sha256', 'common_policy_sha256', 'binary_sha256'}
        if (not isinstance(value, dict) or set(value) != keys
                or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v) for v in value.values())):
            raise Refused('Invalid pipeline executor snapshot')
        if expected is not None and value != expected:
            raise Refused('Pipeline executor or policy hash differs from the recorded invocation')
        return value

    def memory_budget(self):
        import scheduler
        enabled = getattr(self, 'allow_codex_pipeline', False)
        settings = (scheduler.configuration(codex_pipeline=True) if enabled else scheduler.configuration())
        # Control.wrap moves the daemon into agentorchestrator.slice. The
        # supervisor service remains a separate cgroup, not an executor parent.
        return {'daemon_scope_bytes': settings['daemon']['memory_bytes'],
                'supervisor_service_bytes': SUPERVISOR_MEMORY_BYTES,
                'tunnel_service_bytes': TUNNEL_MEMORY_BYTES,
                'separate_cgroups_total_bytes': settings['daemon']['memory_bytes']
                    + SUPERVISOR_MEMORY_BYTES + TUNNEL_MEMORY_BYTES}

    def kernel_task_budget(self):
        import scheduler
        enabled = getattr(self, 'allow_codex_pipeline', False)
        settings = (scheduler.configuration(codex_pipeline=True) if enabled else scheduler.configuration())
        return {'daemon_scope_tasks': settings['daemon']['pids'],
                'supervisor_service_tasks': SUPERVISOR_TASKS, 'tunnel_service_tasks': TUNNEL_TASKS,
                'separate_cgroups_total_tasks': settings['daemon']['pids'] + SUPERVISOR_TASKS + TUNNEL_TASKS}

    @contextmanager
    def locked(self):
        fd = os.open(self.root/'poc-services.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_nlink != 1:
                raise Refused('Unsafe operation lock')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise Refused('Another service operation is in progress')
            yield
        finally: os.close(fd)

    def call(self, argv, timeout=10):
        # Never expose stderr/config text. Commands contain paths, not key values.
        base = Path(ENV['XDG_RUNTIME_DIR'])
        if any(p.is_symlink() for p in [base, *base.parents]): raise Refused('Unsafe user bus ancestry')
        info = base.lstat()
        bus = (base/'bus').lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
                or not stat.S_ISSOCK(bus.st_mode) or bus.st_uid != os.getuid()):
            raise Refused('User bus ownership or permissions are unsafe')
        result = subprocess.run(argv, env=ENV, capture_output=True, text=True, timeout=timeout)
        if result.returncode: raise Refused('Service manager operation failed')
        return result.stdout

    def show(self, name):
        value = self.call(['/usr/bin/systemctl', '--user', 'show', name,
            '-p', 'LoadState', '-p', 'ActiveState', '-p', 'Description', '-p', 'InvocationID', '-p', 'Transient', '-p', 'MainPID'])
        return dict(line.split('=', 1) for line in value.splitlines() if '=' in line)

    def read_state(self):
        path = self.root/STATE
        if not path.exists() and not path.is_symlink(): return None
        private_file(path)
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('runtime') != str(self.root)
                or not isinstance(value.get('token'), str) or not re.fullmatch(r'[0-9a-f]{32}', value['token'])
                or value.get('units') != self.units or not isinstance(value.get('invocations'), dict)
                or set(value['invocations']) - set(self.units)
                or value.get('phase') not in ['STARTING', 'READY', 'STOPPED', 'FAILED', 'CLEANUP_UNCONFIRMED']
                or type(value.get('mailbox_grants_enabled', False)) is not bool
                or type(value.get('codex_pipeline_enabled', False)) is not bool
                or type(value.get('experiments_enabled', False)) is not bool
                or (value.get('experiments_enabled', False) and (
                    not isinstance(value.get('experiment_read_snapshot'), dict)
                    or set(value['experiment_read_snapshot']) != {'read_policy_sha256','manifest_sha256'}
                    or any(not isinstance(v,str) or not re.fullmatch('[a-f0-9]{64}',v) for v in value['experiment_read_snapshot'].values())))
                or (value.get('codex_pipeline_enabled', False) and (
                    not isinstance(value.get('codex_pipeline_snapshot'), dict)
                    or set(value['codex_pipeline_snapshot']) != {'executor_policy_sha256', 'pipeline_policy_sha256', 'common_policy_sha256', 'binary_sha256'}
                    or any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v)
                           for v in value['codex_pipeline_snapshot'].values())))
                or (value.get('mailbox_grants_enabled', False) and value.get('chat_mailbox_enabled') is not True)):
            raise Refused('State ownership record is invalid')
        return value

    def write_state(self, state):
        path = self.root/('poc-services-'+uuid.uuid4().hex+'.tmp')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream); stream.flush(); os.fsync(stream.fileno())
            os.replace(path, self.root/STATE)
        finally:
            if path.exists(): path.unlink()

    def description(self, state, role):
        return 'Agent Orchestrator PoC '+state['token']+' '+role

    def owned(self, state, role, props):
        if props.get('LoadState') == 'not-found': return False
        if (props.get('Transient') != 'yes' or props.get('Description') != self.description(state, role)
                or not props.get('InvocationID') or props['InvocationID'] != state['invocations'].get(role)):
            raise Refused('Unit ownership/invocation mismatch; refusing stop or replacement')
        return True

    def plan(self):
        key = {'path': str(self.key), 'acceptable': False}
        try:
            info = self.key.lstat()
            key.update(owner_uid=info.st_uid, mode=oct(stat.S_IMODE(info.st_mode)),
                       regular=stat.S_ISREG(info.st_mode), symlink=stat.S_ISLNK(info.st_mode))
            credential_metadata(self.key)
            key['acceptable'] = not self.key.is_relative_to(PROJECT)
        except (OSError, Refused): pass
        result = {'runtime': str(self.root), 'mode': MODE, 'units': self.units, 'key_metadata': key,
                'credential_reader_uid': os.getuid(), 'credential_transport': 'native file: reference; no copy or environment value',
                'state_file': str(self.root/STATE), 'logs': str(self.root/'logs'), 'health_file': str(self.health),
                'automatic_start': False, 'survives_terminal_close': True, 'survives_logout_or_reboot': False, 'local_llm_enabled': self.allow_local_llm, 'local_models_enabled': self.allow_local_models,
                'chat_mailbox_enabled': getattr(self, 'allow_chat_mailbox', False),
                'mailbox_grants_enabled': getattr(self, 'allow_mailbox_grants', False),
                'codex_pipeline_enabled': getattr(self, 'allow_codex_pipeline', False)}
        if result['codex_pipeline_enabled']:
            result['codex_pipeline_snapshot'] = self.pipeline_snapshot()
        return result

    def commands(self):
        if getattr(self, 'allow_mailbox_grants', False) and not getattr(self, 'allow_chat_mailbox', False):
            raise Refused('Mailbox grants require the chat mailbox capability')
        daemon = ['/usr/bin/python3', '-B', str(PROJECT/'src/runner.py'), '--runtime', str(self.root),
                  '--codex', str(self.codex), '--development-poc',
                  *(['--allow-codex-pipeline'] if getattr(self, 'allow_codex_pipeline', False) else []), 'daemon']
        adapter = shlex.join(['/usr/bin/env', '-u', 'CONTROL_PLANE_API_KEY', '-u', 'OPENAI_API_KEY', '-u', 'OPENAI_ADMIN_KEY',
                             '/usr/bin/python3', '-B', str(PROJECT/'src/mcp_adapter.py'), '--runtime', str(self.root),
                             *(['--allow-local-llm'] if self.allow_local_llm else []),
                             *(['--allow-local-models'] if getattr(self, 'allow_local_models', False) else []),
                             *(['--allow-chat-mailbox'] if getattr(self, 'allow_chat_mailbox', False) else []),
                             *(['--allow-mailbox-grants'] if getattr(self, 'allow_mailbox_grants', False) else []),
                             *(['--allow-codex-pipeline'] if getattr(self, 'allow_codex_pipeline', False) else []),
                             *(['--allow-experiments'] if getattr(self, 'allow_experiments', False) else [])])
        tunnel = [str(self.client), 'run', '--profile-file', str(PROJECT/'systemd/aceis-mcp-tunnel.yaml'),
                  '--control-plane.base-url=https://api.openai.com',
                  '--control-plane.tunnel-id=tunnel_6ae1d745be7c8191b6d502b613317b4c',
                  '--control-plane.api-key=file:'+str(self.key), '--mcp.command=channel=main,command='+adapter,
                  '--health.listen-addr=127.0.0.1:0', '--health.url-file='+str(self.health),
                  '--log.http-raw-unsafe=false', '--log.level=warn']
        return {'daemon': daemon, 'tunnel': tunnel}

    def require_idle_database(self):
        """Read-only local evidence; never replay unresolved work to recover a service."""
        from codex_host import pipeline_held
        if pipeline_held(self.root):
            raise Refused('Unresolved pipeline work requires reconciliation before service interruption')
        path = self.root/'tasks.sqlite'
        if not path.exists() and (self.root/STATE).exists():
            raise Refused('Recorded task database is missing; reconcile before service interruption')
        if path.exists():
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
                if db.execute("SELECT count(*) FROM tasks WHERE state IN "
                              "('QUEUED','RUNNING','VERIFYING','UNKNOWN','WAITING_FOR_HUMAN')").fetchone()[0]:
                    raise Refused('Unresolved work requires reconciliation before service interruption')

    def recovery_plan(self, rollback_mailbox=False, rollback_grants=False, rollback_pipeline=False, rollback_experiments=False):
        """Return existing CLI argument arrays from recorded configuration, without applying them."""
        with self.locked():
            state = self.read_state()
            if not state or state.get('phase') not in ['READY','STOPPED','FAILED']:
                raise Refused('Missing or uncertain service generation; inspect status before recovery')
            references = []
            for field in ['credential_file_reference','codex_file_reference']:
                value = state.get(field)
                if not isinstance(value,str) or not value or not Path(value).is_absolute():
                    raise Refused('Recorded invocation is incomplete; do not guess file references')
                references.append(Path(value))
            for field in ['local_llm_enabled','local_models_enabled','chat_mailbox_enabled']:
                if type(state.get(field)) is not bool:
                    raise Refused('Recorded invocation is incomplete; do not guess capability flags')
            if type(state.get('mailbox_grants_enabled',False)) is not bool:
                raise Refused('Recorded mailbox grant flag is invalid')
            if state.get('codex_pipeline_enabled', False):
                # Check the recorded binary, never the constructor default.
                from codex_host import executor_snapshot
                if executor_snapshot(self.root, references[1]) != state['codex_pipeline_snapshot']:
                    raise Refused('Pipeline executor or policy hash differs from the recorded invocation')
            observed = {}
            for role, name in self.units.items():
                props = self.show(name)
                present = props.get('LoadState') != 'not-found'
                if present:
                    self.owned(state,role,props)
                    if props.get('ActiveState') not in ['active','inactive','failed']:
                        raise Refused('Service transition is unresolved; do not replay recovery')
                    if state['phase'] in ['STOPPED','FAILED'] and props.get('ActiveState') == 'active':
                        raise Refused('Service state conflicts with the ownership record')
                observed[role] = {'ownership':'owned' if present else 'absent',
                                  'active_state':props.get('ActiveState')}
            if not (self.root/'tasks.sqlite').is_file():
                raise Refused('Recorded task database is missing; reconcile before recovery')
            candidate = type(self)(self.root,references[1],references[0],
                                   state['local_llm_enabled'],state['local_models_enabled'],
                                   False if rollback_mailbox else state['chat_mailbox_enabled'],
                                   False if rollback_mailbox or rollback_grants else state.get('mailbox_grants_enabled',False),
                                   allow_codex_pipeline=False if rollback_pipeline else state.get('codex_pipeline_enabled',False),
                                   allow_experiments=False if rollback_experiments else state.get('experiments_enabled',False))
            checked = candidate.preflight(references[0])
            flags = [flag for enabled,flag in [
                (candidate.allow_local_llm,'--allow-local-llm'),
                (candidate.allow_local_models,'--allow-local-models'),
                (candidate.allow_chat_mailbox,'--allow-chat-mailbox'),
                (candidate.allow_mailbox_grants,'--allow-mailbox-grants'),
                (candidate.allow_codex_pipeline,'--allow-codex-pipeline'),
                (candidate.allow_experiments,'--allow-experiments')] if enabled]
            base = ['/usr/bin/python3','-B',str(PROJECT/'scripts/poc_services.py')]
            common = ['--runtime',str(candidate.root),'--key-file',str(candidate.key),
                      '--codex',str(candidate.codex)]
            commands = {
                'preflight':base+['preflight',*common,'--expected-key-file',str(candidate.key),*flags],
                'stop':base+['stop',*common,'--apply'],
                'start':base+['start',*common,*flags,'--apply'],
                'status':base+['status',*common]}
            return {'operation':'experiment-rollback-plan' if rollback_experiments else ('rollback-plan' if rollback_mailbox else ('grant-rollback-plan' if rollback_grants else ('pipeline-rollback-plan' if rollback_pipeline else 'resume-plan'))),
                    'automatic_apply':False,'revalidate_before_apply':True,
                    'state_sha256':hashlib.sha256((self.root/STATE).read_bytes()).hexdigest(),
                    'observed_units':observed,'preflight':checked,'commands':commands,
                    'credential_contents_read':False,'task_replays':0}

    def preflight(self, expected_key_file=None):
        """No stop/start, DB creation or credential reads; run before interrupting service."""
        from pipeline_policy_migration import assert_migration_ready
        assert_migration_ready(self.root/'codex-pipeline.sqlite3')
        from mcp_adapter import decode, fields, tool_definitions
        previous = self.read_state() or {}
        if previous.get('phase') in ['STARTING','CLEANUP_UNCONFIRMED']:
            raise Refused('Service generation is uncertain; inspect status before recovery')
        expected = expected_key_file or previous.get('credential_file_reference')
        if expected and self.key != Path(expected).expanduser().absolute():
            raise Refused('Credential file reference differs from the existing invocation')
        if previous.get('codex_file_reference') and str(self.codex) != previous['codex_file_reference']:
            raise Refused('Codex file reference differs from the existing invocation')
        for flag in ['allow_local_llm', 'allow_local_models']:
            saved = flag.removeprefix('allow_') + '_enabled'
            if previous.get(saved) is True and not getattr(self, flag):
                raise Refused('Existing capability flag was omitted: '+flag)
        key = credential_metadata(self.key)
        if self.key.is_relative_to(PROJECT): raise Refused('Production key must be outside the repository')
        for binary in [self.codex, self.client]:
            if not binary.is_file() or not os.access(binary, os.X_OK): raise Refused('Executable is missing')
        if not (PROJECT/'systemd/aceis-mcp-tunnel.yaml').is_file(): raise Refused('Tunnel profile is missing')
        self.require_idle_database()
        pipeline_hashes = None
        experiment_hashes = None
        if getattr(self, 'allow_experiments', False):
            from experiments import experiment_snapshot
            experiment_hashes = experiment_snapshot(self.root)
            if previous.get('experiments_enabled',False) and previous['experiment_read_snapshot'] != experiment_hashes:
                raise Refused('Experiment read scope differs from the approved invocation')
        if getattr(self, 'allow_codex_pipeline', False):
            expected_snapshot = previous.get('codex_pipeline_snapshot') if previous.get('codex_pipeline_enabled', False) else None
            pipeline_hashes = self.pipeline_snapshot(expected_snapshot)
        if self.allow_local_llm or self.allow_local_models:
            config_path = self.root/'local-llm.json'
            private_file(config_path,4096)
            config = decode(config_path.read_bytes())
            fields(config,['schema','helper_directory'],['schema','helper_directory'])
            if config['schema'] != 1 or not isinstance(config['helper_directory'],str):
                raise Refused('Invalid local helper configuration')
            helper = Path(config['helper_directory'])
            if (not helper.is_absolute() or any(p.is_symlink() for p in [helper,*helper.parents])
                    or not helper.is_dir() or helper.stat().st_uid != os.getuid()):
                raise Refused('Unsafe helper directory')
            for script in ['extension.py','selection_extension.py']:
                if not (helper/script).is_file(): raise Refused('Local helper entry point is missing')
        policy_hash, slots, grant_hash, grant_task_id = None, 0, None, None
        if self.allow_chat_mailbox:
            from chat_mailbox import Mailbox
            _, allowed, policy_hash = Mailbox.policy_snapshot(self.root)
            slots = len(allowed)
        if getattr(self,'allow_mailbox_grants',False):
            if not self.allow_chat_mailbox:
                raise Refused('Mailbox grants require the chat mailbox capability')
            from mailbox_grants import load_runtime_grant
            # Validate configuration only: expired grants still allow retained answer reads.
            # Never initialize the grant ledger during preflight.
            grant = load_runtime_grant(self.root)
            if any(task_id == grant.task_id for task_id, _ in allowed):
                raise Refused('Grant task collides with a static mailbox task')
            grant_hash, grant_task_id = grant.sha256(), grant.task_id
            if (self.root/'mailbox-grants-prepared.sqlite').exists():
                from mailbox_grants import GrantLedger
                GrantLedger(self.root,grant,enabled=True,create=False).status()
        commands = self.commands()
        if commands['tunnel'].count('--control-plane.api-key=file:'+str(self.key)) != 1:
            raise Refused('Credential argument was not inherited')
        argument = next((v for v in commands['tunnel'] if v.startswith('--mcp.command=channel=main,command=')),None)
        if argument is None: raise Refused('Adapter command is missing')
        adapter = shlex.split(argument.split('command=',2)[-1])
        for attribute, flag in [('allow_local_llm','--allow-local-llm'),
                                ('allow_local_models','--allow-local-models'),
                                ('allow_chat_mailbox','--allow-chat-mailbox'),
                                ('allow_mailbox_grants','--allow-mailbox-grants'),
                                ('allow_codex_pipeline','--allow-codex-pipeline'),
                                ('allow_experiments','--allow-experiments')]:
            if adapter.count(flag) != int(getattr(self,attribute,False)):
                raise Refused('Adapter capability argument was not inherited')
        if commands['daemon'].count('--allow-codex-pipeline') != int(getattr(self, 'allow_codex_pipeline', False)):
            raise Refused('Daemon pipeline capability argument was not inherited')
        if '--allow-writes' in adapter: raise Refused('Task writes are outside this service scope')
        return {'passed':True,'memory_budget': self.memory_budget(), 'kernel_task_budget': self.kernel_task_budget(),
                'credential_file_reference':key['path'],'credential_mode':key['mode'],
                'credential_contents_read':False,'tool_names':[t['name'] for t in tool_definitions(
                    False,self.allow_local_llm,self.allow_local_models,self.allow_chat_mailbox,
                    getattr(self,'allow_codex_pipeline',False),getattr(self,'allow_experiments',False))],
                'experiments_enabled':getattr(self,'allow_experiments',False),
                'experiment_read_snapshot':experiment_hashes,
                'codex_pipeline_enabled':getattr(self,'allow_codex_pipeline',False),
                'codex_pipeline_snapshot':pipeline_hashes,
                'mailbox_policy_sha256':policy_hash,'mailbox_allowed_slots':slots,
                'mailbox_grants_enabled':getattr(self,'allow_mailbox_grants',False),
                'mailbox_grant_policy_sha256':grant_hash,'mailbox_grant_task_id':grant_task_id,
                'stops_or_launches':0,'mailbox_db_created':False,'mailbox_grant_db_created':False}

    def launch(self, state, role, argv):
        name = self.units[role]
        props = {'UMask': '0077', 'KillMode': 'mixed', 'TimeoutStopSec': '60' if role == 'daemon' else '20',
                 'Restart': 'no', 'CPUQuota': '50%' if role == 'daemon' else '25%', 'CPUQuotaPeriodSec': '100ms',
                 'MemoryMax': str(SUPERVISOR_MEMORY_BYTES if role == 'daemon' else TUNNEL_MEMORY_BYTES),
                 'MemorySwapMax': '0', 'TasksMax': str(SUPERVISOR_TASKS if role == 'daemon' else TUNNEL_TASKS),
                 'LimitCORE': '0', 'LimitFSIZE': '64M', 'UnsetEnvironment': UNSET,
                 'StandardOutput': 'append:'+str(self.root/'logs'/('poc-service-'+role+'.log')), 'StandardError': 'inherit'}
        if role == 'tunnel':
            props.update(BindsTo=self.units['daemon'], After=self.units['daemon'], PartOf=self.units['daemon'])
        command = ['/usr/bin/systemd-run', '--user', '--quiet', '--no-ask-password', '--collect', '--no-block',
                   '--expand-environment=no', '--service-type=exec', '--unit='+name,
                   '--description='+self.description(state, role), '--working-directory='+str(PROJECT)]
        for k, v in props.items(): command += ['--property='+k+'='+v]
        # The manager may hold unrelated credentials/target overrides. Pass only
        # explicit nonsecret variables, without importing the invoking terminal.
        clean = ['/usr/bin/env', '-i', *(k+'='+v for k, v in ENV.items())]
        self.call([*command, '--', *clean, *argv])
        # Record only a matching generation. A concurrent foreign unit is never adopted.
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            p = self.show(name)
            if p.get('Description') == self.description(state, role) and p.get('InvocationID'):
                state['invocations'][role] = p['InvocationID']; self.write_state(state); return
            if p.get('LoadState') != 'not-found' and p.get('Description') != self.description(state, role):
                raise Refused('Unexpected unit owner during startup')
            time.sleep(.05)
        raise Refused('Service did not expose its ownership identity')

    def wait_ready(self, role, seconds):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            try:
                remaining = max(.01, min(1, deadline-time.monotonic()))
                if role == 'daemon': ready = daemon_ready(self.root, self.units[role], remaining, manager_env=ENV)
                else: ready = tunnel_ready(self.client, self.health, remaining)
                if ready: return
            except (OSError, ValueError, RuntimeError, EOFError, KeyError, TypeError, subprocess.SubprocessError): pass
            if self.show(self.units[role]).get('ActiveState') not in ['active', 'activating']:
                raise Refused('Service exited before readiness')
            time.sleep(.1)
        raise Refused('Readiness deadline exceeded')

    def stop_owned(self, state):
        # Validate both before stopping either. No PID signalling or lock deletion.
        props = {r: self.show(n) for r, n in self.units.items()}
        owned = {r: self.owned(state, r, p) for r, p in props.items()}
        for role in ['tunnel', 'daemon']:
            if owned[role]: self.call(['/usr/bin/systemctl', '--user', 'stop', self.units[role]], timeout=70)
        for name in self.units.values():
            p = self.show(name)
            if p.get('ActiveState') not in ['inactive', 'failed'] and p.get('LoadState') != 'not-found':
                raise Refused('Service cleanup is not confirmed')

    def start(self, daemon_timeout=20, tunnel_timeout=75):
        with self.locked():
            checked = self.preflight()
            previous = self.read_state()
            for role, name in self.units.items():
                p = self.show(name)
                if p.get('LoadState') != 'not-found':
                    if previous: self.owned(previous, role, p)
                    raise Refused('Service unit already exists; inspect status/stop first')
            if previous and previous.get('phase') not in ['STOPPED', 'FAILED']:
                raise Refused('Stale/incomplete state: inspect status then use stop before start')
            try: socket_path = endpoint(self.root)
            except FileNotFoundError: socket_path = None
            if socket_path is not None and (socket_path.exists() or socket_path.is_symlink()):
                raise Refused('Runtime already has a socket; refusing to replace another daemon')
            credential_metadata(self.key)
            if self.key.is_relative_to(PROJECT): raise Refused('Production key must be outside the repository')
            for binary in [self.codex, self.client]:
                if not binary.is_file() or not os.access(binary, os.X_OK): raise Refused('Executable is missing')
            self.require_idle_database()
            if self.health.exists() or self.health.is_symlink():
                raise Refused('Existing health locator requires inspection; it will not be overwritten')
            state = {'schema': 1, 'runtime': str(self.root), 'token': uuid.uuid4().hex,
                     'units': self.units, 'invocations': {}, 'phase': 'STARTING', 'updated': time.time(),
                     'local_llm_enabled': self.allow_local_llm, 'local_models_enabled': self.allow_local_models,
                     'chat_mailbox_enabled': self.allow_chat_mailbox,
                     'mailbox_grants_enabled': getattr(self,'allow_mailbox_grants',False),
                     'codex_pipeline_enabled': getattr(self,'allow_codex_pipeline',False)}
            if state['codex_pipeline_enabled']:
                state['codex_pipeline_snapshot'] = checked['codex_pipeline_snapshot']
            if getattr(self,'allow_experiments',False):
                state.update(experiments_enabled=True,experiment_read_snapshot=checked['experiment_read_snapshot'])
            state.update(credential_file_reference=str(self.key),codex_file_reference=str(self.codex))
            self.write_state(state)
            try:
                commands = self.commands()
                for role, timeout in [('daemon', daemon_timeout), ('tunnel', tunnel_timeout)]:
                    self.launch(state, role, commands[role]); self.wait_ready(role, timeout)
                state.update(phase='READY', updated=time.time()); self.write_state(state)
                return {'phase': 'READY', 'units': self.units, 'runtime': str(self.root)}
            except BaseException:
                try: self.stop_owned(state)
                except (OSError, Refused, subprocess.SubprocessError):
                    state.update(phase='CLEANUP_UNCONFIRMED', updated=time.time()); self.write_state(state)
                    raise Refused('Startup failed; cleanup unconfirmed; inspect status before any retry') from None
                state.update(phase='FAILED', updated=time.time()); self.write_state(state)
                raise Refused('Startup failed; only owned services were stopped') from None

    def status(self):
        with self.locked():
            state = self.read_state()
            result = {'phase': state['phase'] if state else 'UNMANAGED', 'runtime': str(self.root), 'units': {}}
            for role, name in self.units.items():
                p = self.show(name)
                ownership = 'absent'
                if p.get('LoadState') != 'not-found':
                    try: ownership = 'owned' if state and self.owned(state, role, p) else 'foreign'
                    except Refused: ownership = 'foreign_or_stale'
                result['units'][role] = {'name': name, 'active_state': p.get('ActiveState'), 'ownership': ownership}
            if state and state['phase'] == 'READY' and any(u['ownership'] != 'owned' or u['active_state'] != 'active' for u in result['units'].values()):
                result['phase'] = 'STALE_OR_STOPPED'
            if result['phase'] == 'READY':
                try: result['daemon_ready'] = daemon_ready(self.root, self.units['daemon'], 1, manager_env=ENV)
                except (OSError, ValueError, RuntimeError, EOFError, KeyError, TypeError, subprocess.SubprocessError): result['daemon_ready'] = False
                try: result['tunnel_ready'] = tunnel_ready(self.client, self.health, 2)
                except (OSError, ValueError, RuntimeError, EOFError, KeyError, TypeError, subprocess.SubprocessError): result['tunnel_ready'] = False
                if not result['daemon_ready'] or not result['tunnel_ready']: result['phase'] = 'NOT_READY'
            return result

    def stop(self):
        with self.locked():
            state = self.read_state()
            if not state: raise Refused('No ownership record; refusing to stop unmanaged services')
            self.require_idle_database()
            self.stop_owned(state)
            state.update(phase='STOPPED', updated=time.time()); self.write_state(state)
            return {'phase': 'STOPPED', 'runtime': str(self.root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['plan', 'resume-plan', 'rollback-plan', 'grant-rollback-plan', 'pipeline-rollback-plan', 'experiment-rollback-plan', 'preflight', 'start', 'status', 'stop'])
    parser.add_argument('--runtime', type=Path, default=RUNTIME)
    parser.add_argument('--codex', type=Path, default=CODEX)
    parser.add_argument('--key-file', type=Path, default=KEY)
    parser.add_argument('--expected-key-file', type=Path,
                        help='Known previous credential reference; preflight only, no secret value')
    parser.add_argument('--allow-local-llm', action='store_true',
                        help='Separately approved bounded localhost inference, default off')
    parser.add_argument('--apply', action='store_true', help='Explicitly apply a separately approved start/stop')
    parser.add_argument('--allow-local-models', action='store_true',
                        help='Approved fixed-profile HTTP model lifecycle')
    parser.add_argument('--allow-chat-mailbox', action='store_true',
                        help='Separately approved fixed-slot question/answer mailbox; default off')
    parser.add_argument('--allow-mailbox-grants', action='store_true',
                        help='Separately approved bounded workstream grant; requires --allow-chat-mailbox; default off')
    parser.add_argument('--allow-codex-pipeline', action='store_true',
                        help='Separately approved bounded Codex pipeline; default off')
    parser.add_argument('--allow-experiments', action='store_true', help='Separately approved pinned read-only experiments; default off')
    args = parser.parse_args()
    if args.operation in ['start', 'stop'] and not args.apply:
        parser.error('start/stop require --apply after operator approval')
    os.umask(0o077)
    try:
        options = {'allow_mailbox_grants':True} if args.allow_mailbox_grants else {}
        if args.allow_codex_pipeline: options['allow_codex_pipeline'] = True
        if args.allow_experiments: options['allow_experiments'] = True
        services = Services(args.runtime, args.codex, args.key_file, args.allow_local_llm, args.allow_local_models, args.allow_chat_mailbox, **options)
        if args.operation in ['resume-plan','rollback-plan']:
            result = services.recovery_plan(args.operation == 'rollback-plan')
        elif args.operation == 'grant-rollback-plan':
            result = services.recovery_plan(rollback_grants=True)
        elif args.operation == 'pipeline-rollback-plan':
            result = services.recovery_plan(rollback_pipeline=True)
        elif args.operation == 'experiment-rollback-plan':
            result = services.recovery_plan(rollback_experiments=True)
        elif args.operation == 'preflight':
            result = services.preflight(args.expected_key_file)
        else:
            result = getattr(services,args.operation)()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
        # File contents, environment and external diagnostics never reach stdout.
        print('Operation refused; inspect private metadata/state and bounded service logs.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
