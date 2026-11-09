"""Command execution with a structural deny list.

Every external effect of the controller goes through :class:`Runner`. The deny
list is checked there, not only in prompts, so no code path -- including a future
one that is tricked by PR or issue content -- can merge a pull request, approve
it, resolve a review thread, force push, write labels or prune Docker resources.
"""
import subprocess

from .logging_util import redact


class CommandError(Exception):
    """Command failed or was refused."""


class Denied(CommandError):
    """Command is on the deny list and was never started."""


# Argument subsequences that must never be executed by this controller. They are
# compared case-insensitively: ``gh api -X POST`` and ``gh api -x post`` are the
# same write, and a deny list that only matches one spelling is not a deny list.
DENIED_SEQUENCES = [
    ('gh', 'pr', 'merge'),
    ('gh', 'pr', 'close'),
    ('gh', 'pr', 'review'),
    ('gh', 'pr', 'ready'),
    ('gh', 'pr', 'edit'),
    ('gh', 'issue', 'close'),
    ('gh', 'issue', 'edit'),
    ('gh', 'issue', 'lock'),
    ('gh', 'api', '-X'),                 # any non-default HTTP verb
    ('gh', 'api', '--method'),
    ('gh', 'api', '--field'),
    ('gh', 'api', '--raw-field'),
    ('gh', 'label'),
    ('ao', 'pr', 'merge'),
    ('ao', 'pr', 'resolve-comments'),
    ('ao', 'session', 'kill'),
    ('ao', 'session', 'delete'),
    ('ao', 'session', 'cleanup'),
    ('ao', 'automation', 'create'),
    ('ao', 'automation', 'update'),
    ('ao', 'automation', 'delete'),
    ('git', 'push', '--force'),
    ('git', 'push', '-f'),
    ('git', 'push', '--delete'),
    ('git', 'merge'),
    ('git', 'tag'),
    ('docker', 'system', 'prune'),
    ('docker', 'container', 'prune'),
    ('docker', 'volume', 'prune'),
    ('docker', 'image', 'prune'),
    ('docker', 'compose', 'down'),       # only the wrapper may clean a project
    ('docker', 'compose', 'up'),
    ('docker', 'compose', 'run'),
    ('docker', 'compose', 'build'),
]

# The controller never runs Docker test suites itself; tests belong to workers
# behind the exclusive wrapper lock.
DENIED_COMMAND_PREFIXES = [('docker',), ('docker-compose',)]


def _contains(haystack, needle):
    n = len(needle)
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def assert_allowed(argv):
    lowered = [str(a).lower() for a in argv]
    for seq in DENIED_SEQUENCES:
        if _contains(lowered, [x.lower() for x in seq]):
            raise Denied(f'refused by deny list: {" ".join(argv[:6])}')
    for prefix in DENIED_COMMAND_PREFIXES:
        if lowered[:len(prefix)] == [x.lower() for x in prefix]:
            raise Denied(f'refused by deny list: {" ".join(argv[:3])} '
                         '(Docker work belongs to mrmap-docker-test)')
    return argv


class Runner:
    """Subprocess runner. Tests inject an object with the same ``run`` method."""

    def __init__(self, default_timeout=120):
        self.default_timeout = default_timeout
        self.calls = []

    def run(self, argv, *, timeout=None, check=True):
        argv = [str(a) for a in argv]
        assert_allowed(argv)
        self.calls.append(argv)
        try:
            proc = subprocess.run(argv, text=True, capture_output=True,
                                  timeout=timeout or self.default_timeout)
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f'timeout after {timeout or self.default_timeout}s: '
                               f'{" ".join(argv[:4])}') from exc
        except OSError as exc:
            raise CommandError(f'cannot run {" ".join(argv[:3])}: {exc}') from exc
        if check and proc.returncode != 0:
            raise CommandError(
                f'exit {proc.returncode}: {" ".join(argv[:4])}: '
                f'{redact(proc.stderr)[:300]}')
        return proc
