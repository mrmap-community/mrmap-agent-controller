"""The controller tick.

One tick is one pass over all facts and is safe to interrupt: state lives in
SQLite, external effects go through the runner, and every delivery is reserved by
a unique key before it is attempted. The tick order is always

    1. read facts (GitHub + AO), fail closed on any uncertainty
    2. reconcile claims left over from a crash
    3. intake human-approved issues into new worker sessions
    4. follow pull requests: CI failures and human review feedback

so a worker that a previous tick crashed on is adopted before any new worker is
started.
"""
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from . import db, lifecycle as st
from . import questions as questions_mod
from .ao import AOError, UncertainSpawn
from .github import GithubError, interpret_ci
from .logging_util import redact
from .messages import build_dispatch_message, build_spawn_prompt
from .reviews import ReviewCollector

CLAIM_HOLDING = 'held'
CLAIM_FREE = 'free'


def claim_holds(state):
    """A row in these states blocks any new assignment for the issue."""
    return state != db.CLAIM_RELEASED


def utcnow_factory():
    import datetime as dt

    def _now():
        return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    return _now


class Capacity:
    """Execution-capacity budget for development workers.

    Only sessions whose agent is actually executing consume a slot. An idle or
    exited worker is not charged, but waking it (send on an idle session, or
    resume-agent) is charged, because that is what starts consuming capacity.
    """

    def __init__(self, limit, used, log):
        self.limit = limit
        self.used = used
        self.log = log
        self.notes = []

    def free(self):
        return self.used < self.limit

    def take(self, why):
        self.used += 1
        self.notes.append(f'slot: {why}')

    def summary(self):
        return f'{self.used}/{self.limit} execution slots in use'


class Controller:
    def __init__(self, cfg, gh, ao, database, log, capacity_probe=None,
                 pause_file=None, execute=False, now=None, board_refresher=None):
        self.cfg = cfg
        self.gh = gh
        self.ao = ao
        self.db = database
        self.log = log
        self.execute = execute
        self._now = now or utcnow_factory()
        self.pause_file = Path(pause_file) if pause_file else None
        self.capacity_probe = capacity_probe if capacity_probe is not None \
            else lambda: self._host_capacity()
        # W5: the board refresh is injectable for tests (like capacity_probe);
        # production runs scripts/wip board through lifecycle.refresh_board.
        self.board_refresher = board_refresher if board_refresher is not None \
            else lambda: st.refresh_board(self.cfg, self.log)
        self.claims_changed = False
        # issue -> the claim state reconcile() established for this tick, which
        # is the truth the issues projection must follow even in a dry run.
        self.claim_truth = {}
        self.gh_ok = True
        self.ao_ok = True
        self.actions = []
        self.counts = Counter()
        self.sessions = []
        self.by_issue = defaultdict(list)
        self.open_prs = []
        self.collector = ReviewCollector(cfg, gh, log)

    # ------------------------------------------------------------- helpers --
    def now(self):
        return self._now()

    def event(self, action, reason=None, **fields):
        row = {'action': action, 'reason': redact(reason) if reason else None}
        row.update({k: (redact(v) if isinstance(v, str) else v)
                    for k, v in fields.items()
                    if k in ('issue', 'pr', 'session', 'sha', 'state', 'detail')})
        self.actions.append(row)
        self.counts[action] += 1
        level = _EVENT_LEVELS.get(action, 20)
        self.log.log(level, action, extra={k: v for k, v in row.items()
                                           if k not in ('action',) and v is not None})

    @property
    def controller_paused(self):
        return bool(self.pause_file and self.pause_file.exists())

    def block_reason(self, need_slot=False, capacity=None):
        """Why an external effect cannot happen right now, or None."""
        if not self.execute:
            return 'dry run'
        if self.controller_paused:
            return 'controller pause marker present'
        if not self.gh_ok:
            return 'GitHub state uncertain'
        if not self.ao_ok:
            return 'AO state uncertain'
        if need_slot and capacity is not None and not capacity.free():
            return f'capacity limit reached ({capacity.summary()})'
        return None

    def _host_capacity(self):
        """Ask the repository's host capacity gate. Missing gate = allowed.

        The gate is the same script the Docker test wrapper consults; the
        controller never runs Docker itself.
        """
        script = self.cfg.capacity_script
        if not script:
            return True
        path = Path(script)
        if not path.exists():
            self.log.warning('capacity_gate_missing', detail=str(path))
            return True
        try:
            proc = subprocess.run(['python3', str(path)], capture_output=True,
                                  text=True, timeout=60)
        except Exception as exc:
            self.log.warning('capacity_gate_error', reason=redact(exc))
            return False
        if proc.returncode != 0:
            self.log.warning('capacity_gate_rejected',
                             detail=redact((proc.stdout or '') + (proc.stderr or '')))
            return False
        return True

    # ------------------------------------------------------------ reading ---
    def load_facts(self):
        try:
            self.sessions = self.ao.sessions()
        except AOError as exc:
            self.ao_ok = False
            self.log.error('ao_session_listing_failed', reason=redact(exc))
        else:
            for sess in self.sessions:
                if sess.is_worker:
                    if sess.issue:
                        self.by_issue[sess.issue].append(sess)
                    elif not sess.is_terminated:
                        self.log.warning('worker_without_issue', session=sess.id,
                                         detail='AO session has no issueId; it still '
                                                'consumes capacity')
        try:
            self.open_prs = self.gh.open_pulls()
        except GithubError as exc:
            self.gh_ok = False
            self.log.error('pr_listing_failed', reason=redact(exc))

    def worker_capacity(self):
        executing, foreign = 0, []
        managed = {row['session_id'] for row in db.all_claims(self.db)
                   if row['session_id']}
        for sess in self.sessions:
            if not sess.is_worker or sess.is_terminated or not sess.executing:
                continue
            if sess.id in self.cfg.excluded_sessions:
                continue
            if not self.cfg.count_foreign_workers and sess.id not in managed \
                    and sess.id not in self.by_issue:
                foreign.append(sess.id)
                continue
            executing += 1
        if foreign:
            self.log.debug('foreign_workers_not_counted', detail=','.join(foreign))
        return Capacity(self.cfg.max_workers, executing, self.log)

    def _set_claim(self, issue, state, note, session_id=None):
        """Assignment-board writes only happen in execute mode.

        A dry run reads the shared board and never moves it: the pilot's rows keep
        their state until a real tick decides otherwise.
        """
        if not self.execute:
            self.log.debug('dry_run_claim_update_skipped', issue=issue, state=state,
                           detail=note)
            return
        db.set_claim(self.db, issue, state, self.now(), note=note, session_id=session_id)
        self.claims_changed = True

    def project(self, issue, state, now, **fields):
        """Write the derived ``issues`` projection (W1: the claim is the truth).

        ``issues`` is a projection of ``claims``, never an independent opinion.
        A quarantined claim must not be projected back into a state that says
        work is proceeding, because the follow loop reads pull requests and
        sessions and would happily write ``awaiting_ci`` for a dead worker --
        live example #1009: claim ``needs_reconciliation``, issue
        ``awaiting_review`` on PR #1114, every tick forever. The guard covers
        the whole projection, not only the no-pull-request tail.

        Which claim state applies is decided by :meth:`reconcile` for this tick
        (``claim_truth``) and only read from the table as a fallback: a dry run
        computes an adoption it deliberately does not write, and must not then
        be contradicted by the stale row it left behind.

        Terminal facts are never suppressed -- a merged, closed, paused or
        ready-for-human-merge pull request is information a human needs even
        while its claim sits in quarantine.
        """
        truth = self.claim_truth.get(issue)
        if truth is None:
            claim = db.get_claim(self.db, issue)
            truth = claim['state'] if claim else None
        if truth == db.CLAIM_RECONCILE and state in st.ACTIONABLE:
            state = st.FAILED
            fields['reason'] = 'claim quarantined; ' + (
                fields.get('reason') or 'no deliverable state is projected')
        return db.upsert_issue(self.db, issue, state, now, **fields)

    # ------------------------------------------------------- reconciliation --
    def reconcile(self, capacity):
        """Adopt sessions that exist without a usable claim, and quarantine claims
        whose spawn outcome a crash left unknown."""
        if not self.ao_ok:
            # Without a trustworthy session listing, "no session found" means
            # nothing. Quarantining claims from a failed read would be worse than
            # the crash we are recovering from.
            self.log.warning('reconciliation_skipped',
                             detail='AO session state is unknown this tick')
            return
        for claim in db.all_claims(self.db):
            state, issue = claim['state'], claim['issue']
            if not claim_holds(state):
                continue
            sessions = self.by_issue.get(issue, [])
            live = [s for s in sessions if not s.is_terminated]
            if len(live) > 1:
                self.project(issue, st.FAILED, self.now(),
                             session_id=','.join(s.id for s in live),
                             reason='multiple live AO sessions carry this issueId')
                self.event('NEEDS_HUMAN', issue=issue,
                           reason=f'{len(live)} live sessions claim this issue: '
                                  f'{", ".join(s.id for s in live)}; the '
                                  f'controller will not guess which is responsible')
                continue
            if not live and sessions and state != db.CLAIM_MERGED:
                dead = sessions[0]
                self.claim_truth[issue] = db.CLAIM_RECONCILE
                self._set_claim(issue, db.CLAIM_RECONCILE,
                                f'responsible session {dead.id} is terminated; '
                                f'workspace cleanup={dead.workspace_cleanup or "unknown"}',
                                session_id=dead.id)
                self.project(issue, st.FAILED, self.now(),
                             session_id=dead.id,
                             reason='responsible session terminated')
                self.event('NEEDS_HUMAN', issue=issue, session=dead.id,
                           reason='the AO session carrying this issue is terminated'
                                  + (' (its workspace is already removed)'
                                     if dead.workspace_cleanup == 'removed' else '')
                                  + '; the controller does not restore it and will '
                                    'not start a second worker for the same issue')
                continue
            if live:
                sess = live[0]
                # A live session carrying the issueId outranks any quarantined
                # row: this tick adopts, so the projection must not be clamped
                # to failed by the stale claim the dry run did not rewrite.
                self.claim_truth[issue] = db.CLAIM_SPAWNED
                if state in (db.CLAIM_QUEUED, db.CLAIM_SPAWNING, db.CLAIM_RECONCILE,
                             db.CLAIM_SPAWNED) and claim['session_id'] != sess.id:
                    self._set_claim(issue, db.CLAIM_SPAWNED,
                                    'adopted live AO session', session_id=sess.id)
                    self.event('ADOPTED' if self.execute else 'WOULD_ADOPT',
                               issue=issue, session=sess.id,
                               reason='live AO session carries this issueId but the '
                                      'claim does not record it; association '
                                      + ('restored' if self.execute else 'would be '
                                         'restored'))
                pr_number = self.pr_for(issue, sess)
                self.project(issue,
                             st.PR_OPEN if pr_number else st.RUNNING, self.now(),
                             session_id=sess.id, pr_number=pr_number)
                continue
            # No live session for a held claim.
            existing = db.get_issue(self.db, issue)
            if existing and existing['pr_number']:
                continue                      # pull request tracking handles it
            if state in (db.CLAIM_SPAWNED, db.CLAIM_SPAWNING, db.CLAIM_QUEUED):
                if self._within_grace(claim['created_at']):
                    self.event('PENDING_SPAWN', issue=issue,
                               reason='claim exists without a live session and is '
                                      'still inside the spawn grace window')
                    self.project(issue, st.QUEUED, self.now(),
                                 session_id=claim['session_id'])
                    continue
                self.claim_truth[issue] = db.CLAIM_RECONCILE
                self._set_claim(
                    issue, db.CLAIM_RECONCILE,
                    'no AO session found for this claim; needs '
                    'mrmap-controller release <issue> --confirm to allow a retry',
                    session_id=claim['session_id'])
                self.project(issue, st.FAILED, self.now(),
                             session_id=claim['session_id'],
                             reason='claim without session after grace window')
                self.event('NEEDS_HUMAN' if self.execute else 'WOULD_QUARANTINE',
                           issue=issue,
                           reason='claim exists but no AO session carries the issueId; '
                                  + ('claim quarantined, human decision required'
                                     if self.execute else
                                     'claim would be quarantined for a human decision'))
            elif state == db.CLAIM_RECONCILE:
                self.claim_truth[issue] = db.CLAIM_RECONCILE
                self.project(issue, st.FAILED, self.now(),
                             session_id=claim['session_id'],
                             reason='claim quarantined')

    def _within_grace(self, created_at):
        import datetime as dt
        try:
            created = dt.datetime.fromisoformat(created_at)
        except (TypeError, ValueError):
            return True                       # unparsable: assume recent, do not act
        if created.tzinfo is None:
            created = created.replace(tzinfo=dt.timezone.utc)
        age = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 60.0
        return age < self.cfg.spawn_reconcile_grace_minutes

    # --------------------------------------------------------------- intake --
    def intake(self, capacity):
        try:
            issues = self.gh.open_issues_with_label(self.cfg.approved_label)
        except GithubError as exc:
            self.gh_ok = False
            self.event('INTAKE_BLOCKED', reason=f'approved issue listing failed: {exc}')
            return
        for issue in sorted(issues, key=lambda i: i['number']):
            number = issue['number']
            labels = {l.get('name') for l in issue.get('labels', [])}
            if self.cfg.paused_label in labels:
                db.upsert_issue(self.db, number, st.PAUSED, self.now(),
                                reason=f'{self.cfg.paused_label} label present')
                self.event('SKIP', issue=number, reason='issue is paused')
                continue
            claim = db.get_claim(self.db, number)
            if claim and claim_holds(claim['state']):
                self.event('SKIP', issue=number,
                           reason=f'already claimed (state={claim["state"]}, '
                                  f'session={claim["session_id"]})')
                continue
            live = [s for s in self.by_issue.get(number, []) if not s.is_terminated]
            if live:
                sess = live[0]
                if not claim and self.execute:
                    db.claim_issue(self.db, number, db.CLAIM_SPAWNED, self.now(),
                                   'adopted existing AO session', session_id=sess.id)
                    self.claims_changed = True
                self.event('SKIP', issue=number, session=sess.id,
                           reason='an AO worker session already carries this issueId')
                continue
            prs = [p for p in self.open_prs
                   if self.gh.pr_references_issue(p, number)] if self.gh_ok else []
            if prs:
                self.event('SKIP', issue=number, pr=prs[0].get('number'),
                           reason=f'open PR #{prs[0].get("number")} already '
                                  f'references this issue')
                continue
            if not self.gh_ok:
                self.event('SKIP', issue=number, reason='GitHub state uncertain')
                continue
            ok, reason = self.verify_approval(number)
            if not ok:
                db.upsert_issue(self.db, number, st.FAILED if reason.startswith(
                    'cannot verify') else st.QUEUED, self.now(), reason=reason)
                self.event('REJECT', issue=number, reason=reason)
                continue
            if not self.ao_ok:
                self.event('SKIP', issue=number, reason='AO state uncertain; '
                                                        'no session may be started')
                continue
            if not capacity.free():
                db.upsert_issue(self.db, number, st.QUEUED, self.now(),
                                reason=f'waiting for a slot ({capacity.summary()})')
                self.event('DEFER', issue=number,
                           reason=f'worker limit reached ({capacity.summary()})')
                continue
            if not self.capacity_probe():
                self.event('DEFER', issue=number,
                           reason='host capacity gate rejected a new stack')
                continue
            if not self.execute:
                self.event('WOULD_SPAWN', issue=number, reason=reason)
                continue
            self._spawn(issue, number, reason, capacity)

    def verify_approval(self, number):
        """Latest ``agent: approved`` label event must be a human allowlist add."""
        if not self.cfg.approvers:
            return False, 'no human approvers configured'
        try:
            events = self.gh.issue_label_events(number, self.cfg.approved_label)
        except GithubError as exc:
            return False, f'cannot verify approval label: {redact(exc)}'
        if not events:
            return False, f'no "{self.cfg.approved_label}" label event on this issue'
        last = events[-1]
        if last.get('event') != 'labeled':
            return False, 'the approval label was removed by a later event'
        actor = (last.get('actor') or {}).get('login')
        if actor not in self.cfg.approvers:
            return False, f'last approval label event actor {actor!r} is not an ' \
                          f'allowlisted human'
        return True, f'approved by {actor} at {last.get("created_at")}'

    def _spawn(self, issue, number, reason, capacity):
        if self.controller_paused:
            # The pause marker makes the whole controller report-only, including
            # starts: facts are still read and written down, nothing is launched.
            self.event('DEFER', issue=number,
                       reason='controller pause marker present; start is report-only')
            return
        # From here every path writes the claim row (reserve, re-arm, or move it
        # after a refused/uncertain start); board refresh is owed either way.
        self.claims_changed = True
        title = (issue.get('title') or '')[:80]
        existing = db.get_claim(self.db, number)
        if existing and existing['state'] == db.CLAIM_RELEASED:
            # A release keeps the row for audit; re-arm it instead of inserting a
            # second claim for the same issue.
            db.set_claim(self.db, number, db.CLAIM_SPAWNING, self.now(),
                         note=f're-armed from a released claim: {redact(reason)}',
                         session_id=None)
        elif not db.claim_issue(self.db, number, db.CLAIM_SPAWNING, self.now(),
                                f'reserved by mrmap-controller: {redact(reason)}'):
            self.event('SKIP', issue=number, reason='another claim won the race')
            return
        try:
            fresh = self.gh.issue(number)
            labels = {l.get('name') for l in fresh.get('labels', [])}
            if fresh.get('state') != 'open' or self.cfg.approved_label not in labels \
                    or self.cfg.paused_label in labels:
                db.set_claim(self.db, number, db.CLAIM_RELEASED, self.now(),
                             note='issue labels changed before spawn')
                self.event('SKIP', issue=number,
                           reason=f'labels changed before spawn ({sorted(labels)})')
                return
        except GithubError as exc:
            db.set_claim(self.db, number, db.CLAIM_RECONCILE, self.now(),
                         note=f'cannot re-check labels: {redact(exc)}')
            self.event('NEEDS_HUMAN', issue=number,
                       reason=f'cannot re-check labels before spawn: {redact(exc)}')
            return
        prompt = build_spawn_prompt(self.cfg, number, issue.get('title') or '')
        name = f'Issue #{number}: {title}'
        try:
            output = self.ao.spawn(number, name, prompt)
        except AOError as exc:      # includes UncertainSpawn
            # The claim is kept: a timeout can mean the spawn did happen.
            db.set_claim(self.db, number, db.CLAIM_SPAWNING, self.now(),
                         note=f'spawn outcome unknown: {redact(exc)}')
            db.upsert_issue(self.db, number, st.QUEUED, self.now(), reason=str(exc))
            self.event('SPAWN_UNCERTAIN', issue=number, reason=redact(exc))
            return
        self.ao.refresh()
        try:
            found = self.ao.session_for_issue(number)
            self.by_issue[number] = found
        except AOError as exc:
            self.event('SPAWN_UNCERTAIN', issue=number,
                       reason=f'spawn reported success but the session cannot be '
                              f'located: {redact(exc)}')
            return
        sid = self.ao.session_id_from_output(output)
        if len(found) == 1:
            sess = found[0]
            db.set_claim(self.db, number, db.CLAIM_SPAWNED, self.now(),
                         note=redact(output), session_id=sess.id)
            db.upsert_issue(self.db, number, st.RUNNING, self.now(),
                            session_id=sess.id)
            capacity.take(f'spawn worker for #{number}')
            self.event('SPAWNED', issue=number, session=sess.id, reason=reason)
        elif not found and sid:
            db.set_claim(self.db, number, db.CLAIM_SPAWNED, self.now(),
                         note=redact(output), session_id=sid)
            db.upsert_issue(self.db, number, st.RUNNING, self.now(), session_id=sid,
                            reason='session id parsed from AO output; issueId not '
                                   'visible yet')
            capacity.take(f'spawn worker for #{number}')
            self.event('SPAWNED', issue=number, session=sid,
                       reason='session id parsed from AO output; issueId association '
                              'will be confirmed on the next tick')
        elif not found:
            db.set_claim(self.db, number, db.CLAIM_SPAWNING, self.now(),
                         note='spawn reported success but no session was found')
            self.event('SPAWN_UNCERTAIN', issue=number,
                       reason='spawn command succeeded but no AO session carries the '
                              'issueId; claim kept, next tick reconciles')
        else:
            db.set_claim(self.db, number, db.CLAIM_RECONCILE, self.now(),
                         note='multiple sessions carry this issueId',
                         session_id=','.join(s.id for s in found))
            self.event('NEEDS_HUMAN', issue=number,
                       reason=f'{len(found)} sessions carry this issueId after spawn')

    # ------------------------------------------------------- pull requests --
    def tracked_issues(self):
        issues = {row['issue'] for row in db.all_issues(self.db)}
        issues |= {c['issue'] for c in db.all_claims(self.db)
                   if claim_holds(c['state'])}
        issues |= set(self.by_issue)
        return sorted(issues)

    def pr_for(self, issue, session=None):
        """The pull request that belongs to an issue, or None.

        Order of authority: the association recorded by the controller, then the
        PR list AO itself reports for the responsible session, then a reference
        found in an open pull request. More than one candidate is never resolved
        by guessing.
        """
        row = db.get_issue(self.db, issue)
        if row and row['pr_number']:
            return row['pr_number']
        if session:
            numbers = [n for n in session.pr_numbers if n]
            if len(numbers) == 1:
                return numbers[0]
            if len(numbers) > 1:
                self.event('NEEDS_HUMAN', issue=issue, session=session.id,
                           reason=f'AO reports {len(numbers)} pull requests for this '
                                  f'session ({numbers}); refusing to pick one')
                return None
        matches = [p for p in self.open_prs if self.gh.pr_references_issue(p, issue)]
        numbers = {p.get('number') for p in matches}
        if len(numbers) > 1:
            self.event('NEEDS_HUMAN', issue=issue,
                       reason=f'{len(numbers)} open PRs reference this issue '
                              f'({sorted(numbers)}); refusing to pick one')
            return None
        return next(iter(numbers), None)

    def follow_pull_requests(self, capacity):
        for issue in self.tracked_issues():
            claim = db.get_claim(self.db, issue)
            if claim and claim['state'] == db.CLAIM_MERGED:
                # W1: a merged claim is retired. The row stays for audit and
                # keeps intake away, but the merged pull request is never
                # re-fetched again -- until the incident audit this cost 53
                # `gh pr view` calls every tick, forever.
                continue
            try:
                self._follow_one(issue, capacity)
            except (GithubError, AOError) as exc:
                # One issue's uncertainty must not stop the others; it also must
                # not turn into an action.
                self.event('SKIP', issue=issue, reason=f'uncertain state: {redact(exc)}')
            except Exception as exc:
                # W4: a controller-side error in one issue's follow (a schema
                # problem, a bug, anything) is isolated to that issue. Before
                # this, one poison pull request aborted the whole tick -- 456
                # times. The runs ledger keeps outcome 'ok' and the other pull
                # requests are still followed; the failure gets a bounded
                # backoff instead of a per-tick retry storm.
                self.event('PR_FOLLOW_FAILED', issue=issue, reason=redact(exc))
                self._record_follow_failure(issue, exc)

    def _record_follow_failure(self, issue, exc):
        """Bookkeep a controller-side follow failure and open the retry window.

        The state is one dispatch row per issue, deduped on ``follow_error``
        (a kind no delivery budget reads), so the backoff is restart-safe in
        the same table as the deliveries: ``attempts`` counts occurrences and
        the next retry moves out by a window that doubles from the tick
        interval, capped at 6 hours. Recording failures is the controller's
        own ledger, so it happens in dry-run too.
        """
        key = f'follow_error:{issue}'
        try:
            existing = db.dispatch_for(self.db, key)
            if existing is None:
                row = db.get_issue(self.db, issue)
                db.try_reserve_dispatch(self.db, 'follow_error', issue, key,
                                        self.now(),
                                        pr_number=row['pr_number'] if row else None,
                                        detail=redact(exc))
                attempts = 1
            else:
                db.bump_dispatch(self.db, key)
                attempts = int(existing['attempts']) + 1
            delay = _backoff_delay(self.cfg, attempts)
            db.set_dispatch(self.db, key, 'failed', detail=redact(exc),
                            next_retry_at=_retry_after(self.now(), delay))
        except Exception as record_exc:      # never let bookkeeping mask the bug
            self.log.error('follow_failure_bookkeeping_failed', issue=issue,
                           reason=redact(record_exc))

    def _reconcile_closing(self, issue, pr_number):
        """W2: a merged pull request whose issue is still open gets one ask.

        GitHub's closing linkage (``pr_closes_issue``, the closed-references
        connection) is the fact; the body's prose ``#N`` grep survives only as
        the DB-labeling fallback in :meth:`pr_for`. When a PR merges while its
        issue is still open -- the #981/#995 pattern: body said "What this
        does (#981)", GitHub closed nothing, and intake could re-spawn the
        already-delivered issue -- the controller posts exactly one comment
        naming the merged PR and asks the human to close. Closing is the
        human's act (cf. #1074, closed by jokiefer); the controller never
        closes issues and never merges.

        Dedupe rides on processed_events; a failed post stays unmarked and is
        retried by ``mrmap-controller reconcile-closing`` -- a surface listing
        every merged-PR-with-still-open-issue pair -- rather than re-firing
        per tick, because the retired claim leaves the follow loop here.
        """
        key = f'{pr_number}:{issue}'
        if db.seen(self.db, 'closing_reconcile', key):
            return
        try:
            closes = self.gh.pr_closes_issue(pr_number, issue)
        except GithubError as exc:
            self.log.warning('closing_linkage_unreadable', issue=issue,
                             pr=pr_number, reason=redact(exc))
            return
        if closes:
            db.mark_events(self.db, [('closing_reconcile', key, issue, pr_number,
                                      'closed-by-pr')], self.now())
            self.log.info('closing_linkage_confirmed', issue=issue, pr=pr_number,
                          detail='GitHub closed the issue when the PR merged')
            return
        try:                                  # maybe a human closed it already
            state = (self.gh.issue(issue) or {}).get('state')
        except GithubError as exc:
            self.log.warning('closing_issue_state_unreadable', issue=issue,
                             pr=pr_number, reason=redact(exc))
            return
        if state != 'open':
            db.mark_events(self.db, [('closing_reconcile', key, issue,
                                      pr_number, 'closed-by-human')], self.now())
            self.log.info('closing_reconcile_not_needed', issue=issue,
                          pr=pr_number, detail=f'issue already {state}')
            return
        body = (
            '[mrmap-controller] The merged pull request '
            f'#{pr_number} carries this issue\'s content, but GitHub\'s '
            'closing linkage did not fire and the issue is still open. '
            'If the delivered work covers this issue, please close it (or '
            'close it as superseded and say what replaces it): while the '
            'issue is open with its approval label, intake may hand it to '
            'a new worker. The controller does not close issues itself.')
        if not self.execute:
            self.event('WOULD_RECONCILE_CLOSING', issue=issue, pr=pr_number,
                       reason='merged PR did not close the issue; one ask would be '
                              'posted for a human to close it')
            return
        try:
            self.gh.post_issue_comment(issue, body)
        except GithubError as exc:
            self.event('RECONCILE_CLOSING_FAILED', issue=issue, pr=pr_number,
                       reason=f'ask could not be posted: {redact(exc)}; run '
                              f'"mrmap-controller reconcile-closing" for the '
                              f'current human action list')
            return
        db.mark_events(self.db, [('closing_reconcile', key, issue, pr_number,
                                  'asked-human')], self.now())
        self.event('RECONCILE_CLOSING', issue=issue, pr=pr_number,
                   reason='merged PR left the issue open; exactly one ask posted '
                          'for the human to close it (the controller closes nothing)')

    def _follow_one(self, issue, capacity):
        # W4 backoff gate: a controller-side failure of this issue's follow is
        # retried no earlier than its bounded window says.
        err = db.dispatch_for(self.db, f'follow_error:{issue}')
        if err and err['status'] == 'failed' \
                and _retry_wait(err['next_retry_at'], self.now()):
            self.event('DEFER', issue=issue, pr=err['pr_number'],
                       reason=f'controller-side failure backoff: next attempt '
                              f'no earlier than {err["next_retry_at"]}')
            return
        row = db.get_issue(self.db, issue)
        if row and row['state'] == st.PAUSED:
            paused = True
        else:
            paused = False
            try:
                payload = self.gh.issue(issue)
                labels = {l.get('name') for l in payload.get('labels', [])}
                paused = self.cfg.paused_label in labels
            except GithubError as exc:
                self.log.warning('issue_labels_unavailable', issue=issue,
                                 reason=redact(exc))
        sessions = self.by_issue.get(issue, [])
        live = [s for s in sessions if not s.is_terminated]
        if len(live) == 1:
            session = live[0]
        elif len(sessions) == 1:
            session = sessions[0]          # one session and it is terminated
        else:
            session = None
        if paused:
            self.project(issue, st.PAUSED, self.now(),
                         session_id=session.id if session else None,
                         reason=f'{self.cfg.paused_label} label present')
            self.event('SKIP', issue=issue, reason='issue is paused; no work delivered')
            return
        if len(live) > 1:
            return                                  # reconcile already reported it
        pr_number = self.pr_for(issue, session)
        if pr_number is None:
            claim = db.get_claim(self.db, issue)
            claim_state = claim['state'] if claim else None
            if claim_state in (db.CLAIM_RECONCILE, db.CLAIM_MERGED):
                # W1: the claims row is the source of truth and the issues row
                # is its derived projection. A quarantined claim must never be
                # projected back to an active state by a dead session still
                # carrying the issueId -- until the incident audit, issues #981
                # said running/mrmap-77 forever while its claim read
                # needs_reconciliation. A merged claim never re-opens either.
                if claim_state == db.CLAIM_RECONCILE:
                    self.project(issue, st.FAILED, self.now(),
                                 session_id=claim['session_id'],
                                 reason='claim quarantined; the issues row '
                                        'follows the claim, not the session')
                return
            if session:
                self.project(issue, st.RUNNING, self.now(),
                             session_id=session.id)
            elif row and row['state'] in (st.FAILED, st.MERGED, st.PAUSED):
                pass
            else:
                self.project(issue, st.QUEUED, self.now(),
                             reason='no pull request yet')
            return
        try:
            pr = self.gh.pr_view(pr_number)
        except GithubError as exc:
            self.event('SKIP', issue=issue, pr=pr_number,
                       reason=f'cannot read pull request state: {redact(exc)}')
            return
        if not self.gh.pr_references_issue({'title': pr.get('title'),
                                            'body': '', 'head': {'ref': pr.get('headRefName')},
                                            'number': pr_number}, issue) \
                and not (row and row['pr_number'] == pr_number) \
                and not (session and pr_number in session.pr_numbers):
            self.event('SKIP', issue=issue, pr=pr_number,
                       reason='pull request does not reference this issue')
            return
        head_sha = pr.get('headRefOid')
        if pr.get('state') == 'MERGED':
            self._set_claim(issue, db.CLAIM_MERGED, f'PR #{pr_number} merged',
                            session_id=session.id if session else None)
            self.project(issue, st.MERGED, self.now(), session_id=session.id
                         if session else None, pr_number=pr_number,
                         head_sha=head_sha, ci_status='success',
                         review_state='approved', reason='pull request merged')
            self.event('MERGED' if self.execute else 'WOULD_RETIRE_CLAIM',
                       issue=issue, pr=pr_number,
                       reason='merged by a human; claim '
                              + ('retired' if self.execute else 'would be retired')
                              + ', the controller merges nothing')
            self._reconcile_closing(issue, pr_number)
            return
        if pr.get('state') == 'CLOSED':
            self.project(issue, st.FAILED, self.now(),
                         session_id=session.id if session else None,
                         pr_number=pr_number, reason='pull request closed unmerged')
            self.event('NEEDS_HUMAN', issue=issue, pr=pr_number,
                       reason='pull request closed without merge; the controller '
                              'will not reopen or resubmit')
            return
        ci_status, checks = interpret_ci(pr.get('statusCheckRollup'),
                                         expected_sha=head_sha)
        failures = checks if ci_status == 'failure' else []
        items, dismiss = [], set()
        review_error = None
        try:
            seen = {kind: db.seen_ids(self.db, kind, pr_number)
                    for kind in ('review', 'review_comment', 'issue_comment')}
            items, dismiss = self.collector.collect(pr_number, seen)
        except GithubError as exc:
            review_error = str(redact(exc))
        if dismiss:
            # Read and evaluated, nothing to do: recorded so the same bot comment or
            # approval is not reconsidered forever. Bookkeeping only, no delivery.
            db.mark_events(self.db, [(kind, str(source_id), issue, pr_number, 'read')
                                     for kind, source_id in dismiss], self.now())
        base = dict(session_id=session.id if session else None, pr_number=pr_number,
                    head_sha=head_sha, ci_status=ci_status,
                    review_state='changes_requested' if items
                    else (pr.get('reviewDecision') or 'unknown'))
        if ci_status == 'failure' or items:
            state = self._deliver(issue, session, pr, failures, items,
                                  head_sha, capacity, base)
        elif ci_status == 'pending':
            self.project(issue, st.AWAITING_CI, self.now(),
                         reason='checks still running', **base)
            self.event('WAIT_CI', issue=issue, pr=pr_number, sha=head_sha,
                       reason=f'{len(checks)} check(s) still in progress')
        elif ci_status == 'unknown':
            self.project(issue, st.AWAITING_CI, self.now(),
                         reason='check rollup is empty or belongs to another '
                                'commit; nothing trustworthy to act on', **base)
            self.event('WAIT_CI', issue=issue, pr=pr_number, sha=head_sha,
                       reason='no trustworthy CI result for this head commit')
        else:                                   # CI green
            if review_error:
                self.project(issue, st.AWAITING_REVIEW, self.now(),
                             reason=f'review feedback unreadable: {review_error}',
                             **base)
                self.event('SKIP', issue=issue, pr=pr_number,
                           reason=f'review feedback unreadable: {review_error}')
                return
            if db.count_dispatches(self.db, pr_number, 'review') and not items:
                state = st.AWAITING_REVIEW
            elif (pr.get('reviewDecision') or '').upper() == 'APPROVED' \
                    and str(pr.get('mergeable') or '').upper() in ('MERGEABLE', 'HAS_UNSTABLE',
                                                                   'BEHIND'):
                state = st.READY_FOR_HUMAN_MERGE
            else:
                state = st.AWAITING_REVIEW
            self.project(issue, state, self.now(),
                         reason='CI green; waiting for human review'
                         if state == st.AWAITING_REVIEW
                         else 'CI green and approved; a human merges', **base)
            self.event('IDLE' if state == st.AWAITING_REVIEW else 'READY',
                       issue=issue, pr=pr_number, state=state,
                       reason='nothing for the controller to deliver'
                            + ('; a human merges' if state == st.READY_FOR_HUMAN_MERGE
                               else ''))

    # -------------------------------------------------------------- delivery --
    def _deliver(self, issue, session, pr, failures, items, head_sha,
                 capacity, base):
        pr_number = pr['number']
        kinds = []
        if failures:
            kinds.append('ci')
        if items:
            kinds.append('review')
        kind = 'combined' if len(kinds) == 2 else kinds[0]
        if failures and db.attempt_count(self.db, pr_number, ('ci', 'combined')) \
                >= self.cfg.max_ci_attempts_per_pr:
            self.project(issue, st.FAILED, self.now(),
                         reason=f'CI still failing after '
                                f'{self.cfg.max_ci_attempts_per_pr} delivered fix '
                                f'request(s)', **base)
            self.event('NEEDS_HUMAN', issue=issue, pr=pr_number,
                       reason=f'CI fix attempts exhausted '
                              f'({self.cfg.max_ci_attempts_per_pr}); failing checks: '
                              f'{_check_names(failures)}')
            return st.FAILED
        if items and db.attempt_count(self.db, pr_number, ('review', 'combined')) \
                >= self.cfg.max_review_rounds_per_pr:
            self.project(issue, st.FAILED, self.now(),
                         reason=f'{len(items)} review item(s) remain after '
                                f'{self.cfg.max_review_rounds_per_pr} delivered '
                                f'round(s)', **base)
            self.event('NEEDS_HUMAN', issue=issue, pr=pr_number,
                       reason=f'review rounds exhausted '
                              f'({self.cfg.max_review_rounds_per_pr})')
            return st.FAILED
        key = self._dedupe_key(kind, pr_number, head_sha, items)
        existing = db.dispatch_for(self.db, key)
        if existing and existing['status'] != 'failed':
            state = st.ADDRESSING_REVIEW if 'review' in kind else st.FIXING_CI
            self.project(issue, state, self.now(),
                         reason=f'{kind} work already delivered once for this '
                                f'state (dispatch {existing["id"]}, '
                                f'status {existing["status"]})', **base)
            self.event('ALREADY_DELIVERED', issue=issue, pr=pr_number, sha=head_sha,
                       reason=f'{kind} request already recorded with status '
                              f'{existing["status"]}; not repeated')
            return state
        if session is None:
            self.project(issue, st.FAILED, self.now(),
                         reason=f'work is pending on PR #{pr_number} but no live '
                                f'AO session carries this issue', **base)
            self.event('NEEDS_HUMAN', issue=issue, pr=pr_number,
                       reason=f'{kind} work pending but no responsible worker session')
            return st.FAILED
        if session.is_terminated:
            self.project(issue, st.FAILED, self.now(),
                         session_id=session.id,
                         reason='responsible session is terminated', **base)
            self.event('NEEDS_HUMAN', issue=issue, pr=pr_number, session=session.id,
                       reason='responsible AO session is terminated (workspace may be '
                              'removed); the controller does not restore it')
            return st.FAILED
        if self.controller_paused:
            self.project(issue, st.ADDRESSING_REVIEW if items
                         else st.FIXING_CI, self.now(),
                         reason='controller pause marker present; delivery is '
                                'report-only', **base)
            self.event('DEFER', issue=issue, pr=pr_number, session=session.id,
                       reason='controller pause marker present; delivery is '
                              'report-only')
            return st.ADDRESSING_REVIEW if items else st.FIXING_CI
        needs_slot = not session.executing
        if needs_slot and not capacity.free():
            self.project(issue, st.AWAITING_REVIEW if items else st.AWAITING_CI,
                         self.now(), reason=f'waiting for a slot: '
                                            f'{capacity.summary()}', **base)
            self.event('DEFER', issue=issue, pr=pr_number, session=session.id,
                       reason=f'worker limit reached ({capacity.summary()}); {kind} '
                              f'work waits for a slot instead of being dropped')
            return st.AWAITING_CI
        message = build_dispatch_message(
            self.cfg, issue=issue, pr_number=pr_number, pr_url=pr.get('url'),
            head_sha=head_sha, ci_failures=failures, review_items=items,
            mode='execute' if self.execute else 'dry-run')
        if not self.execute:
            self.event('WOULD_DISPATCH', issue=issue, pr=pr_number, session=session.id,
                       sha=head_sha, detail=f'{kind} ({len(failures)} check(s), '
                                            f'{len(items)} review item(s))')
            return st.ADDRESSING_REVIEW if items else st.FIXING_CI
        if not existing and not db.try_reserve_dispatch(
                self.db, kind, issue, key, self.now(), pr_number=pr_number,
                head_sha=head_sha,
                detail=f'{len(failures)} check(s), {len(items)} item(s)'):
            self.event('ALREADY_DELIVERED', issue=issue, pr=pr_number,
                       reason='reserved by a concurrent tick')
            return st.FIXING_CI
        if existing:
            db.bump_dispatch(self.db, key)
            self.log.info('retrying_failed_delivery', issue=issue, pr=pr_number,
                          detail=f'{kind} request {existing["id"]} failed before; '
                                 f'attempt counted against the retry limit')
        ok, detail = self._send(session, message, kind)
        if ok:
            db.set_dispatch(self.db, key, 'dispatched', session_id=session.id)
            self._mark_seen(issue, pr_number, items, head_sha, key)
            if needs_slot:
                capacity.take(f'woke worker for PR #{pr_number}')
            state = st.ADDRESSING_REVIEW if items else st.FIXING_CI
            self.project(issue, state, self.now(), reason=detail, **base)
            self.event('DISPATCHED', issue=issue, pr=pr_number, session=session.id,
                       sha=head_sha, detail=f'{kind}: {detail}')
            return state
        if detail == 'uncertain':
            db.set_dispatch(self.db, key, 'uncertain', session_id=session.id)
            self._mark_seen(issue, pr_number, items, head_sha, key)
            self.project(issue, st.FAILED, self.now(),
                         reason='delivery outcome unknown; not retried '
                                'automatically', **base)
            self.event('NEEDS_HUMAN', issue=issue, pr=pr_number, session=session.id,
                       reason='AO delivery outcome unknown; the controller will not '
                              'risk a duplicate resume')
            return st.FAILED
        # A definite failure stays recorded with the same dedupe key: the retry
        # limit counts it, so an undeliverable item cannot loop forever.
        db.set_dispatch(self.db, key, 'failed', session_id=session.id, detail=detail)
        self.project(issue, st.FIXING_CI if failures else st.AWAITING_REVIEW,
                     self.now(), reason=f'delivery failed: {detail}', **base)
        self.event('DISPATCH_FAILED', issue=issue, pr=pr_number, session=session.id,
                   reason=detail)
        return st.FIXING_CI if failures else st.AWAITING_REVIEW

    def _send(self, session, message, kind):
        """Wake the responsible worker if needed, then deliver once."""
        if session.resumable:
            try:
                self.ao.resume_agent(session.id)
            except AOError as exc:
                self.log.error('resume_failed', session=session.id, reason=redact(exc))
                return False, f'cannot resume exited agent: {redact(exc)}'
            self.log.info('agent_resumed', session=session.id)
        try:
            self.ao.send(session.id, message)
        except UncertainSpawn as exc:
            self.log.error('delivery_uncertain', session=session.id,
                           reason=redact(exc))
            return False, 'uncertain'
        except AOError as exc:
            self.log.error('delivery_failed', session=session.id, reason=redact(exc))
            return False, redact(exc)
        return True, f'{kind} request delivered'

    def _mark_seen(self, issue, pr_number, items, head_sha, key):
        """Record exactly what this delivery covered: the head SHA and the ids."""
        rows = [('ci_head', f'{pr_number}:{head_sha}', issue, pr_number, key)] \
            if head_sha else []
        rows += [(i.kind if i.kind != 'inline comment' else 'review_comment',
                  i.source_id, issue, pr_number, key) for i in items]
        db.mark_events(self.db, rows, self.now())

    def _dedupe_key(self, kind, pr_number, head_sha, items):
        if kind == 'ci':
            return f'ci:{pr_number}:{head_sha}'
        if kind == 'review':
            ids = ','.join(sorted(i.source_id for i in items))
            digest = hashlib.sha256(ids.encode()).hexdigest()[:16]
            return f'review:{pr_number}:{digest}'
        ids = ','.join(sorted(i.source_id for i in items))
        digest = hashlib.sha256(ids.encode()).hexdigest()[:16]
        return f'combined:{pr_number}:{head_sha}:{digest}'

    # ---------------------------------------------------------- questions ----
    def questions(self):
        """W3: answered human questions wake their waiting workers.

        One line in the tick, all logic in questions.py: the two machine
        question protocols on GitHub, the verification of scripts/decisions,
        and a wake through the same AO layer that delivers CI and review
        work. A broken question stage must never eat a tick, so every failure
        inside it lands in the log instead of the run ledger.
        """
        try:
            return questions_mod.run(self)
        except Exception as exc:
            self.log.error('questions_stage_failed', reason=redact(exc))
            return None

    # ------------------------------------------------------------------ run --
    def tick(self):
        mode = 'execute' if self.execute else 'dry-run'
        run_id = db.start_run(self.db, mode, self.now())
        self.log.info('tick_start', mode=mode, detail=f'dry run of the controller'
                      if not self.execute else 'controller executing')
        outcome = 'ok'
        try:
            self.load_facts()
            capacity = self.worker_capacity()
            self.log.info('capacity', mode=mode, detail=capacity.summary())
            self.reconcile(capacity)
            self.intake(capacity)
            self.follow_pull_requests(capacity)
            self.questions()
            if self.execute and self.claims_changed:
                # W5: board #917 is only regenerated by scripts/wip commands,
                # so a tick that moved a claim must ask wip for a refresh --
                # the board's format stays wip's, duplicated nowhere here.
                try:
                    self.board_refresher()
                except Exception as exc:     # a board problem never eats a tick
                    self.log.warning('board_refresh_failed', reason=redact(exc))
            if self.controller_paused:
                self.log.warning('controller_paused', mode=mode,
                                 detail='pause marker present; nothing was executed')
        except Exception as exc:                       # never lose the run record
            outcome = 'aborted'
            self.event('ABORTED', reason=redact(exc))
            self.log.error('tick_aborted', mode=mode, reason=redact(exc))
            raise
        finally:
            db.finish_run(self.db, run_id, self.now(), outcome,
                          json.dumps(dict(self.counts), sort_keys=True))
        self.log.info('tick_end', mode=mode, outcome=outcome,
                      detail=json.dumps(dict(self.counts), sort_keys=True))
        return {'mode': mode, 'counts': dict(self.counts), 'actions': self.actions,
                'capacity': capacity.summary()}


def _check_names(failures):
    return ', '.join(f'{f.get("name")}({f.get("conclusion")})' for f in failures[:8])


#: W4: controller-side retries double from one tick interval and stop at 6 h.
BACKOFF_CAP_SECONDS = 6 * 3600


def _backoff_delay(cfg, attempts):
    """Seconds to wait before the ``attempts``-th controller-side retry."""
    interval = max(60, int(cfg.poll_interval_minutes) * 60)
    return min(interval * 2 ** max(0, int(attempts) - 1), BACKOFF_CAP_SECONDS)


def _retry_after(now_iso, delay_seconds):
    import datetime as dt
    base = dt.datetime.fromisoformat(now_iso)
    return (base + dt.timedelta(seconds=delay_seconds)).isoformat(timespec='seconds')


def _retry_wait(next_retry_iso, now_iso):
    """Seconds remaining until ``next_retry_iso``, or None when nothing gates.

    Unparsable or empty timestamps never gate: a broken backoff marker must
    not become a silent stall -- the tick retries as it always did.
    """
    import datetime as dt
    if not next_retry_iso:
        return None
    try:
        target = dt.datetime.fromisoformat(next_retry_iso)
        now = dt.datetime.fromisoformat(now_iso)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    remaining = (target - now).total_seconds()
    return remaining if remaining > 0 else None


def loop_watchdog(database, gh, log, board_issue, threshold=3):
    """Alert once when ``threshold`` consecutive finished ticks aborted (W4).

    Called by the loop after every tick, healthy or not. On a streak it writes
    one CRITICAL line -- visible in ``logs/loop.log`` (the loop's stderr) and
    ``logs/controller.jsonl`` -- and posts exactly one comment on the board
    issue through the gh layer. Dedupe rides on processed_events and is keyed
    by ``db.abort_streak``'s anchor, so a continuing stall cannot spam the
    board and a recovered stall may alert again. A failed post stays unmarked
    so the next tick retries it. Nothing is ever restarted: the loop is the
    operator's decision, not the controller's.
    """
    count, anchor = db.abort_streak(database)
    if count < threshold:
        return False
    key = f'stall:{anchor}'
    if db.seen(database, 'loop_stall', key):
        return False
    log.critical('loop_stalled',
                 detail=f'{count} consecutive ticks aborted since run #{anchor}; '
                        f'the loop process is alive but its ticks fail -- see '
                        f'event tick_aborted in logs/controller.jsonl')
    if board_issue:
        try:
            gh.post_issue_comment(int(board_issue), (
                '[mrmap-controller] WATCHDOG: the scheduler loop aborted '
                f'{count} consecutive ticks (streak after run #{anchor}). The '
                'loop process keeps running but its ticks fail; inspect the '
                '"tick_aborted" events in logs/controller.jsonl and the runs '
                'table of the state database. The controller does not restart '
                'itself, and this alert is posted once per stall episode.'))
        except GithubError as exc:
            log.error('loop_stall_alert_failed', reason=redact(exc))
            return False                   # unmarked: next tick retries
    db.mark_events(database, [('loop_stall', key, None, None, 'watchdog')],
                   utcnow_factory()())
    return True


_EVENT_LEVELS = {
    # Informational outcomes log at INFO; everything an operator should look at
    # logs at WARNING, and a controller-side failure logs at ERROR.
    'NEEDS_HUMAN': 40, 'ABORTED': 40, 'DISPATCH_FAILED': 40, 'INTAKE_BLOCKED': 40,
    'PR_FOLLOW_FAILED': 40, 'QUESTION_WAKE_FAILED': 40, 'QUESTION_NO_WORKER': 40,
    'RECONCILE_CLOSING_FAILED': 40,
    'WOULD_QUARANTINE': 30, 'WOULD_RECONCILE_CLOSING': 30,
    'SPAWN_UNCERTAIN': 30, 'REJECT': 30, 'DEFER': 30,
    'QUESTION_GAP': 30, 'QUESTION_UNVERIFIED': 30,
}
