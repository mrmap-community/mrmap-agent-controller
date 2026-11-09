"""Configuration for the MrMap Agent Controller.

Config is a JSON file (default ``<install>/config.json``) with environment
overrides for the handful of values an operator flips during rollout. Nothing
secret is ever read from or written to the config file: GitHub authentication is
inherited from the existing ``gh`` login and is never logged.
"""
import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# The controller shares the pilot's state database and the pilot's exclusive
# intake lock on purpose: the legacy gatekeeper and the controller then cannot
# start two workers for one issue, whichever of them runs first.
LEGACY_ROOT = Path('/home/ota/.ao/mrmap-gatekeeper')

DEFAULTS = {
    # identity
    'repo': 'mrmap-community/mrmap',
    'ao_project': 'mrmap',
    # storage (shared with the working pilot; additive schema only)
    'state_db': str(LEGACY_ROOT / 'state.sqlite3'),
    'lock_file': str(LEGACY_ROOT / 'intake.lock'),
    'log_dir': str(BASE / 'logs'),
    'backup_dir': str(BASE / 'backups'),
    # rollout
    'dry_run': True,
    'max_workers': 1,
    'poll_interval_minutes': 5,
    # approval policy: only these humans may approve an issue with a label
    'approved_label': 'agent: approved',
    'paused_label': 'agent: paused',
    'approvers': ['jokiefer'],
    # who may direct a worker through PR feedback
    'review_allowlist': ['jokiefer'],
    'review_associations': ['OWNER', 'MEMBER', 'COLLABORATOR'],
    'ignore_authors': ['mrmap-agent-bot'],
    # retries
    'max_ci_attempts_per_pr': 2,
    'max_review_rounds_per_pr': 4,
    # crash recovery
    'spawn_reconcile_grace_minutes': 20,
    # worker environment
    'docker_test_wrapper': '/home/ota/.ao/bin/mrmap-docker-test',
    'capacity_script': '/home/ota/agent-orchestrator-test/mrmap/scripts/agent_capacity',
    # work board refresh (W5) and stall alerting (W4)
    'wip_script': '/home/ota/agent-orchestrator-test/mrmap/scripts/wip',
    'board_issue': 917,
    # behaviour switches
    'count_foreign_workers': True,
    'allow_restore_terminated': False,
    'use_thread_resolution': True,
    'excluded_sessions': ['mrmap-1'],
    'message_max_chars': 12000,
    'quote_max_chars': 700,
    'command_timeout_seconds': 120,
}

_INT_KEYS = ('max_workers', 'poll_interval_minutes', 'max_ci_attempts_per_pr',
             'max_review_rounds_per_pr', 'spawn_reconcile_grace_minutes',
             'command_timeout_seconds', 'message_max_chars', 'quote_max_chars',
             'board_issue')
_BOOL_KEYS = ('dry_run', 'count_foreign_workers', 'allow_restore_terminated',
              'use_thread_resolution')
_LIST_KEYS = ('approvers', 'review_allowlist', 'review_associations',
              'ignore_authors', 'excluded_sessions')


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, values):
        self._v = values

    def __getattr__(self, name):
        try:
            return self._v[name]
        except KeyError:
            raise ConfigError(f'unknown config key: {name}') from None

    def as_dict(self):
        return dict(self._v)

    @property
    def public(self):
        """Config for logs and the status command (no secrets exist anyway)."""
        return dict(self._v)


def _coerce(key, raw):
    if key in _INT_KEYS:
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f'{key} must be an integer, got {raw!r}') from None
    if key in _BOOL_KEYS:
        if isinstance(raw, bool):
            return raw
        if str(raw).strip().lower() in ('1', 'true', 'yes', 'on'):
            return True
        if str(raw).strip().lower() in ('0', 'false', 'no', 'off'):
            return False
        raise ConfigError(f'{key} must be a boolean, got {raw!r}')
    if key in _LIST_KEYS:
        if isinstance(raw, str):
            return [x.strip() for x in raw.split(',') if x.strip()]
        if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
            return list(raw)
        raise ConfigError(f'{key} must be a list of strings, got {raw!r}')
    return raw


def load(path=None, env=None):
    """Load config from file plus ``MRMAP_CONTROLLER_*`` overrides."""
    env = os.environ if env is None else env
    path = Path(path or env.get('MRMAP_CONTROLLER_CONFIG') or (BASE / 'config.json'))
    values = dict(DEFAULTS)
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except ValueError as exc:
            raise ConfigError(f'{path}: invalid JSON: {exc}') from exc
        if not isinstance(raw, dict):
            raise ConfigError(f'{path}: top level must be an object')
        unknown = sorted(set(raw) - set(DEFAULTS))
        if unknown:
            raise ConfigError(f'{path}: unknown keys: {", ".join(unknown)}')
        for key, val in raw.items():
            values[key] = _coerce(key, val)
    elif path != BASE / 'config.json' and env.get('MRMAP_CONTROLLER_CONFIG'):
        raise ConfigError(f'MRMAP_CONTROLLER_CONFIG points at a missing file: {path}')
    for key in DEFAULTS:
        env_name = 'MRMAP_CONTROLLER_' + key.upper()
        if env_name in env:
            values[key] = _coerce(key, env[env_name])
    cfg = Config(values)
    cfg._source = str(path)
    _validate(cfg)
    return cfg


def _validate(cfg):
    if cfg.max_workers < 1:
        raise ConfigError('max_workers must be at least 1')
    if cfg.max_workers > 3:
        raise ConfigError('max_workers is capped at 3 (host capacity)')
    if cfg.poll_interval_minutes < 1:
        raise ConfigError('poll_interval_minutes must be at least 1')
    if not cfg.repo or '/' not in cfg.repo:
        raise ConfigError('repo must be owner/name')
    if not cfg.approvers:
        raise ConfigError('approvers must not be empty: approvals need a human allowlist')
    if not Path(cfg.state_db).parent.is_dir():
        raise ConfigError(f'state_db parent directory missing: {cfg.state_db}')
