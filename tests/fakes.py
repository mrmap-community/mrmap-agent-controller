"""Offline test doubles for GitHub and AO, plus a controller harness.

No test here touches the network, the real state database, the real AO daemon or
GitHub. Everything the controller can see is what these objects return, and each
fake records every call so a test can assert what was and was not attempted.
"""
import datetime as dt
import logging
import pathlib
import tempfile

from mrmap_controller import config as config_mod
from mrmap_controller import db
from mrmap_controller.ao import AOError, Session, UncertainSpawn
from mrmap_controller.core import Controller
from mrmap_controller.github import GithubError


def now(offset_minutes=0):
    return (dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(minutes=offset_minutes)).isoformat(timespec='seconds')


class Recording:
    def __init__(self):
        self.calls = []

    def note(self, call_name, **fields):
        self.calls.append((call_name, fields))

    def names(self):
        return [name for name, _ in self.calls]

    def count(self, call_name):
        return self.names().count(call_name)

    def with_name(self, call_name):
        return [fields for name, fields in self.calls if name == call_name]


class FakeGitHub(Recording):
    """Stands in for mrmap_controller.github.GitHub."""

    # Reuse the production reference test so the fakes cannot hide a matching bug.
    pr_references_issue = staticmethod(__import__(
        'mrmap_controller.github', fromlist=['GitHub']).GitHub.pr_references_issue)

    def __init__(self, issues=None, events=None, pulls=None, prs=None, reviews=None,
                 review_comments=None, issue_comments=None, threads=None, login='mrmap-agent-bot',
                 closed_links=None, parents=None):
        super().__init__()
        self.issues = issues or []
        self.events = events or {}
        self.pulls = pulls or []
        self.prs = prs or {}
        self.reviews = reviews or {}
        self.review_comments = review_comments or {}
        # GitHub serves PR conversation comments and plain issue comments from
        # one endpoint in one shared numbering space, so one map covers both.
        self.comments_by_number = issue_comments or {}
        self.threads = threads or {}
        # pr number -> issue numbers GitHub's closing linkage says that PR closed
        # (W2): the closed_references payload of the fakes.
        self.closed_links = closed_links or {}
        # issue number -> native sub-issue parent number (W3 epics)
        self.parents = parents or {}
        self.login = login
        self.fail_on = set()
        # number -> exception: pr_view raises exactly this for that one pull
        # request (W4 per-PR isolation tests feed it a sqlite OperationalError).
        self.pr_view_raises = {}
        # every watchdog board alert posted during the test
        self.posted_comments = []
        # Deliberately never read by the controller: native blocking relationships
        # must not influence scheduling. Tests set this to prove it.
        self.blocking_by_issue = {}

    def _maybe_fail(self, name):
        if name in self.fail_on:
            raise GithubError(f'simulated failure in {name}')

    def whoami(self):
        return self.login

    def open_issues_with_label(self, label):
        self._maybe_fail('open_issues_with_label')
        self.note('open_issues_with_label', label=label)
        return [i for i in self.issues if i.get('state') == 'open'
                and label in {l['name'] for l in i.get('labels', [])}]

    def issue(self, number):
        self._maybe_fail('issue')
        self.note('issue', number=number)
        for item in self.issues:
            if item['number'] == number:
                return item
        raise GithubError(f'issue #{number} not in fixture')

    def issue_label_events(self, number, label):
        self._maybe_fail('issue_label_events')
        self.note('issue_label_events', number=number, label=label)
        return list(self.events.get(number, []))

    def open_pulls(self):
        self._maybe_fail('open_pulls')
        self.note('open_pulls')
        return list(self.pulls)

    def pr_view(self, number):
        self.note('pr_view', number=number)
        if number in self.pr_view_raises:
            # The call genuinely happened and broke controller-side: recorded
            # first, like a gh command that ran and then failed underneath.
            raise self.pr_view_raises[number]
        self._maybe_fail('pr_view')
        if number not in self.prs:
            raise GithubError(f'PR #{number} not in fixture')
        return dict(self.prs[number])

    def pr_reviews(self, number):
        self._maybe_fail('pr_reviews')
        self.note('pr_reviews', number=number)
        return list(self.reviews.get(number, []))

    def pr_review_comments(self, number):
        self._maybe_fail('pr_review_comments')
        self.note('pr_review_comments', number=number)
        return list(self.review_comments.get(number, []))

    def pr_issue_comments(self, number):
        self._maybe_fail('pr_issue_comments')
        self.note('pr_issue_comments', number=number)
        return list(self.comments_by_number.get(number, []))

    def issue_comments(self, number):
        # Same REST endpoint as pr_issue_comments -- issues and PRs share the
        # numbering space; W3 reads the author_association of each comment.
        self._maybe_fail('issue_comments')
        self.note('issue_comments', number=number)
        return list(self.comments_by_number.get(number, []))

    def issue_parent(self, number):
        self._maybe_fail('issue_parent')
        self.note('issue_parent', number=number)
        return self.parents.get(number)

    def pr_closes_issue(self, pr_number, issue):
        self._maybe_fail('pr_closes_issue')
        self.note('pr_closes_issue', pr=pr_number, issue=issue)
        return int(issue) in {int(n) for n in self.closed_links.get(pr_number, set())}

    def pr_resolved_thread_comment_ids(self, number):
        self._maybe_fail('pr_resolved_thread_comment_ids')
        self.note('pr_resolved_thread_comment_ids', number=number)
        # Same contract as the real adapter: string comment ids.
        return {str(x) for x in self.threads.get(number, set())}

    def post_issue_comment(self, number, body):
        # The watchdog board alert. No test reaches GitHub through here.
        self._maybe_fail('post_issue_comment')
        self.note('post_issue_comment', number=number, body=body)
        self.posted_comments.append((number, body))
        return f'posted to #{number}'


class FakeAO(Recording):
    """Stands in for mrmap_controller.ao.AO, including the deny of bad resumes."""

    def __init__(self, sessions=None, project='mrmap'):
        super().__init__()
        self.project = project
        self.sessions_list = [s if isinstance(s, Session) else Session(s)
                              for s in (sessions or [])]
        self.fail_on = set()
        self.spawn_creates = True
        self.spawn_creates_count = 1
        self.next_session_number = 90
        self.resumes = []
        self.sends = []
        self.send_uncertain = False
        self.send_fails = False
        self.resume_fails = False

    def refresh(self):
        pass

    def _maybe_fail(self, name, exc=AOError):
        if name in self.fail_on:
            raise exc(f'simulated failure in {name}')

    def sessions(self, include_terminated=True, enrich=True):
        self._maybe_fail('sessions')
        self.note('sessions')
        return list(self.sessions_list)

    def session(self, session_id):
        for sess in self.sessions_list:
            if sess.id == session_id:
                return sess
        raise AOError(f'no such session {session_id}')

    def session_for_issue(self, issue):
        self._maybe_fail('session_for_issue')
        self.note('session_for_issue', issue=issue)
        return [s for s in self.sessions_list
                if s.issue == int(issue) and not s.is_terminated]

    def spawn(self, issue, name, prompt):
        # A spawn that cannot be confirmed is never a clean failure: the real
        # adapter raises UncertainSpawn for both timeouts and transport errors.
        # The call is recorded first because the command really was started.
        self.note('spawn', issue=issue, name=name, prompt=prompt)
        self._maybe_fail('spawn', UncertainSpawn)
        if self.spawn_creates:
            created = []
            for _ in range(self.spawn_creates_count):
                sess = Session({'id': f'{self.project}-{self.next_session_number}',
                                'projectId': self.project, 'kind': 'worker',
                                'issueId': str(issue), 'activity': {'state': 'active'},
                                'isTerminated': False, 'prs': []})
                self.next_session_number += 1
                self.sessions_list.append(sess)
                created.append(sess.id)
            return 'spawned session ' + ', '.join(created)
        return 'spawn accepted'

    def session_id_from_output(self, output):
        import re
        match = re.search(rf'\b{self.project}-\d+\b', output or '')
        return match.group(0) if match else None

    def resume_agent(self, session_id):
        self._maybe_fail('resume_agent')
        self.note('resume_agent', session=session_id)
        if self.resume_fails:
            raise AOError(f'resume of {session_id} refused')
        for sess in self.sessions_list:
            if sess.id == session_id:
                sess.raw['activity'] = {'state': 'idle'}
                sess.activity = 'idle'
        self.resumes.append(session_id)
        return self.session(session_id)

    def send(self, session_id, message, client_message_id=None):
        self._maybe_fail('send')
        self.note('send', session=session_id, message=message)
        if self.send_uncertain:
            raise UncertainSpawn('simulated timeout while delivering')
        if self.send_fails:
            raise AOError('session is not running')
        self.sends.append((session_id, message))
        return 'delivered'

    def automations(self):
        return []


class FakeRunner:
    """Stands in for mrmap_controller.runner.Runner when testing adapters."""

    def __init__(self, responses=None, deny=()):
        self.responses = responses or {}
        self.calls = []
        self.deny = deny
        self.raise_on = {}

    def run(self, argv, *, timeout=None, check=True):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        key = ' '.join(argv)
        for pattern, exc in self.raise_on.items():
            if pattern in key:
                raise exc
        for prefix in self.deny:
            if key.startswith(prefix):
                raise AssertionError(f'unexpected command: {key}')
        if key not in self.responses:
            raise AssertionError(f'unexpected command: {key}')
        stdout = self.responses[key]
        return type('P', (), {'stdout': stdout, 'stderr': '', 'returncode': 0})()


class Harness:
    """A controller wired to fakes, a temporary database and a silent logger."""

    def __init__(self, gh=None, ao=None, execute=False, config_overrides=None,
                 tmp=None):
        # Passing an existing tmp reuses that directory, and with it the state
        # database: tests pre-seed claims and then drive another tick.
        self.tmp = pathlib.Path(tmp) if tmp else pathlib.Path(
            tempfile.mkdtemp(prefix='mrmap-controller-test-'))
        self.tmp.mkdir(parents=True, exist_ok=True)
        overrides = {
            'state_db': str(self.tmp / 'state.sqlite3'),
            'lock_file': str(self.tmp / 'intake.lock'),
            'log_dir': str(self.tmp / 'logs'),
            'backup_dir': str(self.tmp / 'backups'),
            'capacity_script': '',
            'dry_run': not execute,
            'max_workers': 1,
            'spawn_reconcile_grace_minutes': 20,
            'docker_test_wrapper': '/home/ota/.ao/bin/mrmap-docker-test',
        }
        overrides.update(config_overrides or {})
        values = dict(config_mod.DEFAULTS)
        values.update(overrides)
        self.cfg = config_mod.Config(values)
        config_mod._validate(self.cfg)
        self.conn = db.connect(self.cfg.state_db, durable=False)
        db.init(self.conn)
        self.log = _Capturing()
        self.gh = gh or FakeGitHub()
        self.ao = ao or FakeAO()
        self.execute = execute
        self.capacity_ok = True
        self.pause_file = self.tmp / 'paused'
        # W5: the harness records board refreshes instead of running wip; no
        # test may spawn subprocesses against the real repository.
        self.board_calls = []

    def controller(self, execute=None):
        return Controller(self.cfg, self.gh, self.ao, self.conn, self.log,
                          capacity_probe=lambda: self.capacity_ok,
                          pause_file=self.pause_file,
                          board_refresher=lambda: self.board_calls.append(True),
                          execute=self.execute if execute is None else execute)

    def bind(self, gh=None, ao=None):
        if gh is not None:
            self.gh = gh
        if ao is not None:
            self.ao = ao
        return self

    def tick(self, execute=None, gh=None, ao=None):
        """Run one tick, optionally with fresh doubles. The shared logger keeps
        collecting, so assertions can use either the report or ``h.log``."""
        self.bind(gh=gh, ao=ao)
        return self.controller(execute=execute).tick()

    def claim(self, issue, state='spawned', session_id=None, note='', age_minutes=0):
        db.claim_issue(self.conn, issue, state, now(-age_minutes), note,
                       session_id=session_id)

    def rows(self):
        return {r['issue']: r for r in db.all_issues(self.conn)}

    def close(self):
        self.conn.close()


def actions(report, name=None):
    return [a for a in report['actions']
            if name is None or a['action'] == name]


def reasons(report, name):
    return ' | '.join(str(a.get('reason') or a.get('detail') or '')
                      for a in actions(report, name))


class _Capturing:
    """Logger double: keeps (level, event, fields) and never writes files."""

    def __init__(self):
        self.records = []

    def log(self, level, event, extra=None, **fields):
        merged = dict(extra or {})
        merged.update(fields)
        self.records.append((level, event, merged))

    def __getattr__(self, name):
        def emit(event, **fields):
            level = {'debug': 10, 'info': 20, 'warning': 30,
                     'error': 40, 'critical': 50}.get(name, 20)
            self.log(level, event, **fields)
        return emit

    def events(self, name=None):
        return [r for r in self.records if name is None or r[1] == name]

    def reasons(self, name=None):
        return ' | '.join(r[2].get('reason', '') or r[2].get('detail', '')
                          for r in self.events(name))


# ------------------------------------------------------------- fixtures -----
def approved_issue(number=120, title='Do the thing', labels=('agent: approved',),
                   state='open'):
    return {'number': number, 'title': title, 'state': state,
            'labels': [{'name': name} for name in labels]}


def label_event(event, actor, event_id=1, when='2026-11-06T10:00:00Z', label='agent: approved'):
    return {'event': event, 'id': event_id, 'created_at': when,
            'label': {'name': label}, 'actor': {'login': actor}}


def session(session_id, issue=None, activity='active', terminated=False, prs=None,
            role='worker'):
    return {'id': session_id, 'projectId': 'mrmap', 'kind': role,
            'issueId': str(issue) if issue is not None else None,
            'activity': {'state': activity}, 'isTerminated': terminated,
            'prs': prs or []}


def pr(number=500, issue=120, state='OPEN', sha='a' * 40, checks=None,
       review_decision='REVIEW_REQUIRED', mergeable='MERGEABLE', head=None):
    head = head or f'ao/issue-{issue}/thing'
    return {'number': number, 'state': state, 'isDraft': False,
            'headRefName': head, 'headRefOid': sha, 'mergedAt': None,
            'url': f'https://github.example/pull/{number}',
            'title': f'implements thing (#{issue})',
            'reviewDecision': review_decision, 'mergeable': mergeable,
            'mergeStateStatus': 'CLEAN',
            'statusCheckRollup': checks if checks is not None else [
                {'name': 'unit-tests', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}],
            'author': {'login': 'mrmap-agent-bot'}, 'updatedAt': now()}


def open_pr_entry(number=500, issue=120, head=None, title=None):
    head = head or f'ao/issue-{issue}/thing'
    return {'number': number, 'title': title or f'thing (#{issue})', 'body': '',
            'head': {'ref': head}}


def failing_checks(name='unit-tests'):
    return [{'name': name, 'status': 'COMPLETED', 'conclusion': 'FAILURE',
             'detailsUrl': f'https://github.example/runs/{name}'}]


def pending_checks():
    return [{'name': 'acceptance-tests', 'status': 'IN_PROGRESS', 'conclusion': None}]


def human_review(review_id=9001, state='CHANGES_REQUESTED', body='please fix X',
                 login='jokiefer', association='MEMBER'):
    return {'id': review_id, 'state': state, 'body': body,
            'submitted_at': now(), 'html_url': 'https://github.example/r',
            'author_association': association,
            'user': {'login': login, 'type': 'User'}}


def inline_comment(comment_id=9101, body='line 12 is wrong', login='jokiefer',
                   association='MEMBER', path='backend/x.py'):
    return {'id': comment_id, 'body': body, 'path': path, 'line': 12,
            'html_url': 'https://github.example/c',
            'author_association': association,
            'user': {'login': login, 'type': 'User'}}


def bot_comment(comment_id=9201, login='sonarqubecloud[bot]',
                body='S123: refactor this'):
    return {'id': comment_id, 'body': body, 'path': 'backend/x.py', 'line': 12,
            'html_url': 'https://github.example/b', 'author_association': 'NONE',
            'user': {'login': login, 'type': 'Bot'}}
