"""AO CLI adapter.

Capabilities were verified against AO 0.14.9 on this host:

* ``ao session ls --json`` has no ``issueId`` -- only ``ao session get`` exposes
  the issue association, so issue-to-session lookup reads each live session.
* ``ao send`` reaches a session whose agent is connected (``active``/``idle``).
* An agent that exited inside a live session (``activity.state == "exited"``,
  ``isTerminated == false``) is restarted with ``ao session resume-agent``.
* A terminated session is never restored automatically: ``ao session get`` may
  report ``workspaceCleanup: removed``, meaning its worktree is gone and the
  work cannot be continued in place. That is reported to a human instead.
"""
import json
import re

from .logging_util import redact

EXECUTING_STATES = {'active', 'working', 'busy', 'running'}
IDLE_STATES = {'idle'}
EXITED_STATES = {'exited', 'stopped', 'unknown'}


class AOError(Exception):
    """AO could not be queried or acted on reliably."""


class UncertainSpawn(AOError):
    """A spawn may or may not have happened; never retry it blindly."""


class Session:
    def __init__(self, data, source='get'):
        self.raw = data or {}
        self.source = source
        self.id = self.raw.get('id')
        self.project = self.raw.get('projectId')
        self.role = self.raw.get('kind') or self.raw.get('role')
        self.display_name = self.raw.get('displayName') or ''
        self.status = self.raw.get('status') or ''
        activity = self.raw.get('activity')
        if isinstance(activity, dict):
            self.activity = (activity.get('state') or 'unknown').lower()
        else:
            self.activity = (activity or 'unknown').lower()
        self.is_terminated = bool(self.raw.get('isTerminated'))
        self.prs = self.raw.get('prs') or []
        self.branch = self.raw.get('branch') or ''
        self.workspace_cleanup = self.raw.get('workspaceCleanup') or ''
        issue = self.raw.get('issueId')
        try:
            self.issue = int(issue) if issue not in (None, '') else None
        except (TypeError, ValueError):
            self.issue = None

    @property
    def is_worker(self):
        return self.role == 'worker'

    @property
    def executing(self):
        """True only when the agent is consuming execution capacity right now."""
        return not self.is_terminated and self.activity in EXECUTING_STATES

    @property
    def connected(self):
        return not self.is_terminated and self.activity in (EXECUTING_STATES | IDLE_STATES)

    @property
    def resumable(self):
        return not self.is_terminated and self.activity in EXITED_STATES

    @property
    def pr_numbers(self):
        return [p.get('number') for p in self.prs if isinstance(p, dict)
                and p.get('number')]

    def __repr__(self):
        return (f'Session({self.id}, issue={self.issue}, activity={self.activity!r}, '
                f'terminated={self.is_terminated})')


class AO:
    def __init__(self, runner, project, timeout=120):
        self.runner = runner
        self.project = project
        self.timeout = timeout
        self._details = {}

    def _json(self, argv, timeout=None):
        try:
            proc = self.runner.run(argv, timeout=timeout)
        except Exception as exc:
            raise AOError(redact(exc)) from exc
        try:
            return json.loads(proc.stdout)
        except ValueError as exc:
            raise AOError(f'unparsable AO response from {" ".join(argv[:4])}') from exc

    # ------------------------------------------------------------ reading ---
    def sessions(self, include_terminated=True, enrich=True):
        argv = ['ao', 'session', 'ls', '--project', self.project, '--json']
        if include_terminated:
            argv.append('--include-terminated')
        data = self._json(argv)
        rows = data.get('data') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise AOError('unexpected AO session listing')
        sessions = [Session(row, 'ls') for row in rows if isinstance(row, dict)]
        if enrich:
            # issueId is only in the detail payload; fetch it for live workers.
            for sess in sessions:
                if sess.is_worker and not sess.is_terminated:
                    detail = self.session(sess.id)
                    if detail:
                        sess.__dict__.update(detail.__dict__)
                        sess.source = 'get'
        return sessions

    def refresh(self):
        """Drop the per-tick detail cache so the next read sees fresh state."""
        self._details.clear()

    def session(self, session_id):
        if session_id in self._details:
            return self._details[session_id]
        data = self._json(['ao', 'session', 'get', str(session_id),
                           '--project', self.project, '--json'])
        if not isinstance(data, dict) or not isinstance(data.get('session'), dict):
            raise AOError(f'unexpected AO session payload for {session_id}')
        sess = Session(data['session'])
        self._details[session_id] = sess
        return sess

    def session_for_issue(self, issue):
        """All live worker sessions whose issueId equals ``issue``.

        More than one is a contradiction in AO's own bookkeeping and callers must
        not guess: it is reported, never silently resolved.
        """
        found = []
        for sess in self.sessions():
            if not sess.is_worker or sess.is_terminated:
                continue
            detail = self.session(sess.id)
            if detail.issue == int(issue):
                found.append(detail)
        return found

    def daemon_status(self):
        return self._json(['ao', 'status', '--json'])

    def automations(self):
        data = self._json(['ao', 'automation', 'list', '--json'])
        return (data or {}).get('automations') or []

    # ------------------------------------------------------------ acting ----
    def spawn(self, issue, name, prompt, branch=None):
        """Open a worker session for one issue, on that issue's branch.

        The branch is passed explicitly rather than left to AO's default:
        the default is named after the session, so two sessions for the same
        issue would work on two different branches, and the identity that
        binds issue, session and branch would be lost.
        """
        argv = ['ao', 'spawn', '--project', self.project, '--kind', 'worker',
                '--issue', str(issue), '--name', name[:100], '--prompt', prompt]
        if branch:
            argv += ['--branch', branch]
        try:
            proc = self.runner.run(argv, timeout=max(self.timeout, 300))
        except Exception as exc:
            raise UncertainSpawn(f'spawn outcome unknown: {redact(exc)}') from exc
        return (proc.stdout or '').strip()

    def spawn_branch_option_supported(self):
        """Whether the installed ``ao`` accepts ``spawn --branch`` (W6).

        Asked of the binary rather than read off a version string: the flag is
        what the identity contract needs, and an AO that silently ignored it
        would start every worker on a session-named branch again.
        """
        try:
            proc = self.runner.run(['ao', 'spawn', '--help'], timeout=30)
        except Exception as exc:
            raise AOError(f'cannot ask ao spawn for its options: '
                          f'{redact(exc)}') from exc
        return '--branch' in ((proc.stdout or '') + (proc.stderr or ''))

    def resume_agent(self, session_id):
        """Restart an exited agent inside its existing session."""
        argv = ['ao', 'session', 'resume-agent', str(session_id),
                '--project', self.project, '--json']
        try:
            self._json(argv, timeout=max(self.timeout, 180))
        except AOError as exc:
            raise AOError(f'resume-agent {session_id} failed: {redact(exc)}') from exc
        self._details.pop(session_id, None)
        sess = self.session(session_id)
        if not sess.connected:
            raise AOError(f'resume-agent {session_id} reported no connected agent '
                          f'(activity={sess.activity!r})')
        return sess

    def send(self, session_id, message, client_message_id=None):
        argv = ['ao', 'send', '--session', str(session_id), '--message', message]
        if client_message_id:
            argv += ['--client-message-id', client_message_id]
        try:
            proc = self.runner.run(argv, timeout=max(self.timeout, 180))
        except Exception as exc:
            raise UncertainSpawn(f'send outcome unknown for {session_id}: '
                                 f'{redact(exc)}') from exc
        return (proc.stdout or '').strip()

    def session_id_from_output(self, output):
        match = re.search(rf'\b{re.escape(self.project)}-\d+\b', output or '')
        return match.group(0) if match else None
