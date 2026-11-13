"""Bounded role recommendations and profile validation, without model execution."""
from copy import deepcopy
import json
from pathlib import Path
import tomllib

ROLES = {
    'Implementer': ('implementer', 'own worktree only'),
    'ImplementerQuick': ('implementer_quick', 'own worktree only'),
    'ImplementerAdvanced': ('implementer_advanced', 'own worktree only'),
    'Orchestrator': ('orchestrator', 'none'),
    'IndependentReviewer': ('independent_reviewer', 'none'),
    'SecurityReviewer': ('security_reviewer', 'none'),
    'TestReviewer': ('test_reviewer', 'none'),
    'PerformanceReviewer': ('performance_reviewer', 'none'),
    'DocumentationReviewer': ('documentation_reviewer', 'none'),
    'Verifier': ('verifier', 'verification outputs only'),
    'Researcher': ('researcher', 'none'),
    'Coordinator': ('parent', 'integration only'),
}
MODELS = {'gpt-6.1-sol', 'gpt-6-luna'}
PRIMARY_MODEL = 'gpt-6.1-sol'
REVIEW_ROLES = {
    'correctness': 'IndependentReviewer',
    'security': 'SecurityReviewer',
    'tests': 'TestReviewer',
    'performance': 'PerformanceReviewer',
    'documentation': 'DocumentationReviewer',
}
BUDGET_CEILINGS = {
    'cloud_concurrency': 6, 'local_concurrency': 1,
    'max_dispatches': 24, 'max_retries': 2,
    'task_timeout_s': 300, 'run_timeout_s': 900,
}


def configured_budgets(project=None, *, requested=None, executor_limit=None):
    """Resolve a new-run default; never change active leases or claim capacity discovery."""
    configured, source = 'max', 'profile_default'
    if project is not None:
        path = Path(project) / 'config/native-team.json'
        if any(part.is_symlink() for part in [path, *path.parents]):
            raise ValueError('UNSAFE_NATIVE_SETTINGS_PATH')
        if path.exists():
            if not path.is_file() or path.stat().st_size > 4096:
                raise ValueError('INVALID_NATIVE_SETTINGS')
            raw = path.read_bytes()
            if len(raw) > 4096:
                raise ValueError('INVALID_NATIVE_SETTINGS')

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('DUPLICATE_NATIVE_SETTING')
                    result[key] = value
                return result

            try:
                settings = json.loads(raw.decode('utf-8'), object_pairs_hook=unique)
            except (UnicodeError, json.JSONDecodeError, RecursionError):
                raise ValueError('INVALID_NATIVE_SETTINGS') from None
            if (not isinstance(settings, dict)
                    or set(settings) != {'schema_version', 'cloud_concurrency'}
                    or type(settings['schema_version']) is not int
                    or settings['schema_version'] != 1):
                raise ValueError('INVALID_NATIVE_SETTINGS')
            configured, source = settings['cloud_concurrency'], 'project_config'
    ceiling = BUDGET_CEILINGS['cloud_concurrency']

    def valid(value):
        return value == 'max' if type(value) is str else type(value) is int and 1 <= value <= ceiling

    if not valid(configured) or (requested is not None and not valid(requested)):
        raise ValueError('INVALID_NATIVE_CONCURRENCY')
    if executor_limit is not None and (type(executor_limit) is not int or executor_limit < 1):
        raise ValueError('INVALID_EXECUTOR_LIMIT')
    selected = configured if requested is None else requested
    effective = min(ceiling, ceiling if selected == 'max' else selected,
                    ceiling if executor_limit is None else executor_limit)
    budgets = deepcopy(BUDGET_CEILINGS)
    budgets['cloud_concurrency'] = effective
    return budgets, {'configured': configured, 'requested': selected, 'effective': effective,
                     'project_ceiling': ceiling, 'executor_limit': executor_limit,
                     'executor_limit_verified': False,
                     'executor_limit_source': 'not_reported' if executor_limit is None else 'caller_reported',
                     'selection_source': source if requested is None else 'override',
                     'active_runs_changed': False}


def role_registry():
    return {name: {'native_role': native, 'writes': writes}
            for name, (native, writes) in ROLES.items()}


def load_profile(project, role):
    """Read one bounded TOML profile. Caller supplies symlink/path guards."""
    if role not in ROLES or role == 'Coordinator':
        raise ValueError('UNSUPPORTED_ROLE_PROFILE')
    native = ROLES[role][0]
    path = Path(project) / '.codex' / 'agents' / (native + '.toml')
    if path.stat().st_size > 65536:
        raise ValueError('ROLE_SIZE_LIMIT')
    raw = path.read_bytes()
    value = tomllib.loads(raw.decode('utf-8'))
    if (set(value) != {'name', 'description', 'developer_instructions',
                       'model', 'model_reasoning_effort'}
            or value['name'] != native
            or any(not isinstance(v, str) or not v.strip() for v in value.values())
            or value['model'] not in MODELS
            or value['model_reasoning_effort'] != 'max'):
        raise ValueError('UNSUPPORTED_ROLE_SETTINGS')
    return raw, value


def recommend(purpose, complexity='standard', *, parallel_parts=1,
              security_sensitive=False, performance_sensitive=False,
              cross_project=False, project=None, cloud_concurrency=None,
              executor_limit=None):
    """Select roles, not actors or permissions. The parent constructs the DAG."""
    if purpose not in {'implementation', 'documentation', 'research', 'orchestration'}:
        raise ValueError('UNKNOWN_TASK_PURPOSE')
    if complexity not in {'quick', 'standard', 'advanced'}:
        raise ValueError('UNKNOWN_TASK_COMPLEXITY')
    if type(parallel_parts) is not int or not 1 <= parallel_parts <= 24:
        raise ValueError('INVALID_PARALLEL_PARTS')
    if any(type(v) is not bool for v in
           [security_sensitive, performance_sensitive, cross_project]):
        raise ValueError('BOOLEAN_RISK_FLAGS_REQUIRED')
    budgets, concurrency = configured_budgets(project, requested=cloud_concurrency,
                                             executor_limit=executor_limit)
    if purpose in {'research', 'orchestration'} and parallel_parts != 1:
        raise ValueError('SINGLE_ADVISORY_ASSIGNMENT_REQUIRED')
    planning = (purpose != 'orchestration'
                and (parallel_parts > 1 or cross_project or complexity == 'advanced'))
    primary = {
        'implementation': {'quick': 'ImplementerQuick', 'standard': 'Implementer',
                           'advanced': 'ImplementerAdvanced'}[complexity],
        'documentation': 'ImplementerQuick',
        'research': 'Researcher',
        'orchestration': 'Orchestrator',
    }[purpose]
    reviews = []
    if purpose == 'implementation':
        reviews.extend(['IndependentReviewer', 'TestReviewer'])
    elif purpose == 'documentation':
        reviews.append('DocumentationReviewer')
    if security_sensitive:
        reviews.append('SecurityReviewer')
    if performance_sensitive:
        reviews.append('PerformanceReviewer')
    assignments = []
    if planning:
        assignments.append({'phase': 'plan', 'role': 'Orchestrator', 'count': 1})
    assignments.append({'phase': 'work', 'role': primary, 'count': parallel_parts})
    assignments.extend({'phase': 'review', 'role': role, 'count': 1} for role in reviews)
    if purpose in {'implementation', 'documentation'}:
        assignments.append({'phase': 'verify', 'role': 'Verifier', 'count': 1})
    # Coordinator admission is a dispatch too, even though it does not spawn a child.
    integration_dispatches = int(purpose in {'implementation', 'documentation'})
    dispatches = sum(item['count'] for item in assignments) + integration_dispatches
    if dispatches > budgets['max_dispatches']:
        raise ValueError('RECOMMENDATION_EXCEEDS_DISPATCH_BUDGET')
    return {'assignments': assignments,
            'budgets': budgets, 'concurrency': concurrency,
            'suggested_concurrency': min(parallel_parts, budgets['cloud_concurrency']),
            'required_dispatches': dispatches,
            'parent_integration_dispatches': integration_dispatches,
            'automatic_dispatch': False, 'automatic_retry': False,
            'availability_verified': False}
