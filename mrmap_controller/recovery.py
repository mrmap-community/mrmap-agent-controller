"""What each quarantined work item actually is, and what a human can do next (W8).

A quarantine is a refusal to guess, which is correct and also, on its own,
useless: after the crashes of 2026-11 seven claims sat in
``needs_reconciliation`` (#829 #948 #981 #1009 #1032 #1078 #1085) and nothing
could say which of them were finished work, which were waiting for a human,
which could be woken up and which were genuinely ambiguous.

This module answers that. It reads the same facts a tick reads -- claim,
projection, GitHub issue and labels, linked pull requests and their CI/review
state, AO session liveness and branch identity -- and states them in a fixed
order, then sorts the item into exactly one kind. It never acts, and a verdict
that can be acted on says so with the command a *human* runs. The one action the
controller will take on a verdict (:func:`recover`) is the narrow, reversible
class: waking a session that is merely sleeping, on an ownership check.
"""
from . import db, identity, lifecycle as st
from .github import GithubError, interpret_ci
from .questions import KIND_DECISION_REQUEST, KIND_DECISION_REQUEST_OBSERVED

# The kinds a verdict can have. Kept as strings: they are printed, grepped and
# compared by operators, not only by code.
UNCERTAIN = 'facts_uncertain'
NOTHING = 'nothing_to_reconcile'
LIVE = 'live_worker'
AWAITING_PR = 'awaiting_pr_review'
READY = 'ready_for_human_merge'
COMPLETED = 'completed_merged_unconfirmed'
CLOSED = 'issue_closed'
UNAPPROVED = 'not_approved'
RESUMABLE = 'safely_resumable'
ORPHANED = 'orphaned_claim'
PENDING = 'pending_spawn'
WAITING_HUMAN = 'waiting_for_human_answer'
AMBIGUOUS = 'ambiguous_needs_human'

ALL_KINDS = (UNCERTAIN, NOTHING, LIVE, AWAITING_PR, READY, COMPLETED, CLOSED,
            UNAPPROVED, RESUMABLE, ORPHANED, PENDING, WAITING_HUMAN, AMBIGUOUS)

#: The only kind ``recover`` accepts. Everything else is either finished, owned
#: by a live worker, or a question a human has to answer.
RECOVERABLE = RESUMABLE


class Verdict:
    """One work item, explained. ``evidence`` reads top-down in the order the
    facts were checked; ``action`` is a command line a human runs, or None."""

    def __init__(self, issue, kind, evidence=None, action=None, session_id=None):
        self.issue = issue
        self.kind = kind
        self.evidence = evidence or []
        self.action = action
        self.session_id = session_id

    def __repr__(self):
        return f'Verdict(#{self.issue}, {self.kind}, action={self.action!r})'

    def as_dict(self):
        return {'issue': self.issue, 'kind': self.kind, 'evidence': self.evidence,
                'action': self.action, 'session_id': self.session_id}

    def line(self, explain=False):
        head = f'#{self.issue:<6} {self.kind:<28}'
        if not explain:
            return head + (f' -> {self.action}' if self.action else '')
        lines = [head + ('  ' if self.action else '')]
        for fact in self.evidence:
            lines.append(f'         {fact}')
        if self.action:
            lines.append(f'         action: {self.action}')
        else:
            lines.append('         action: none; the loop or a human already owns this')
        return '\n'.join(lines)


def assess(ctrl, issue):
    """One verdict, from the facts ``ctrl`` has already loaded.

    The order is deliberate: a live worker first (nothing is stuck while an
    agent is executing), then GitHub's own facts, then the pull request, then
    the session. A quarantined claim whose work is sitting on an open pull
    request is not "a dead worker" -- it is finished work waiting for a human,
    and reading it the other way round is what made #1009 look broken forever.
    """
    evidence = []
    release = f'mrmap-controller release {issue} --confirm'

    def verdict(kind, extra=(), action=None, session_id=None):
        return Verdict(issue, kind, evidence + list(extra), action, session_id)

    if not ctrl.gh_ok or not ctrl.ao_ok:
        missing = ', '.join(n for n, ok in (('GitHub', ctrl.gh_ok), ('AO', ctrl.ao_ok))
                            if not ok)
        return verdict(UNCERTAIN, [f'{missing} state could not be read this tick; '
                                   f'nothing here can be trusted'],
                       'mrmap-controller doctor')

    claim = db.get_claim(ctrl.db, issue)
    row = db.get_issue(ctrl.db, issue)
    if claim is None:
        return verdict(NOTHING, ['no claim row: intake owns this issue, there is '
                                 'nothing to recover'])
    evidence.append(f'claim: {claim["state"]} '
                    f'(session {claim["session_id"] or "none recorded"}, '
                    f'created {claim["created_at"]})')
    if row:
        evidence.append(f'projection: {row["state"]}'
                        + (f', PR #{row["pr_number"]}' if row['pr_number'] else '')
                        + (f', reason: {row["reason"]}' if row['reason'] else ''))

    try:
        gh_issue = ctrl.gh.issue(issue)
    except GithubError as exc:
        return verdict(UNCERTAIN, [f'issue #{issue} unreadable: {exc}'], 'doctor')
    labels = {l.get('name') for l in gh_issue.get('labels', [])}
    evidence.append(f'github: {gh_issue.get("state")}, labels '
                    f'{sorted(labels) or "none"}')
    if gh_issue.get('state') != 'open':
        return verdict(CLOSED, ['GitHub says the issue is closed; the claim is '
                                'history, not a blocker'], release)
    if ctrl.cfg.paused_label in labels:
        return verdict(UNAPPROVED, [f'{ctrl.cfg.paused_label} is on it: paused work is '
                                    f'intended, not stuck'],
                       f'mrmap-controller resume-issue {issue}')
    approved, why = ctrl.verify_approval(issue)
    evidence.append(f'approval: {why}')
    if not approved:
        return verdict(UNAPPROVED, ['no current human approval on the issue; a '
                                    'worker may not be started for it'],
                       f'a human adds {ctrl.cfg.approved_label}, or ' + release)

    live = [s for s in ctrl.by_issue.get(issue, []) if not s.is_terminated]
    if len(live) > 1:
        return verdict(AMBIGUOUS, [f'{len(live)} live sessions carry this issueId '
                                   f'({", ".join(s.id for s in live)}); picking one '
                                   f'would put work on the wrong agent'])
    if live:
        sess = live[0]
        ok, contradiction = identity.verify_assignment(issue, sess.branch)
        if not ok:
            return verdict(AMBIGUOUS, [contradiction], session_id=sess.id)
        branch = sess.branch or 'a branch AO named itself'
        if sess.connected:
            return verdict(LIVE, [f'session {sess.id} is live on {branch} (activity '
                                  f'{sess.activity}); the loop owns this, nothing to '
                                  f'recover'],
                           session_id=sess.id)
        prs = _open_pulls(ctrl, issue, evidence)
        if len(prs) > 1:
            return verdict(AMBIGUOUS, ['more than one open pull request for one '
                                       'issue; the controller never selects one'])
        if prs:
            kind, action = _pr_state(ctrl, prs[0], evidence)
            return verdict(kind, action=action + release, session_id=sess.id)
        if sess.resumable:
            capacity = ctrl.worker_capacity()
            if not capacity.free():
                return verdict(RESUMABLE, [f'session {sess.id} is {sess.activity} and '
                                           f'resumable, but {capacity.summary()}'],
                               f'wait for a free slot, then: recover {issue} --confirm',
                               sess.id)
            return verdict(RESUMABLE, [f'session {sess.id} is {sess.activity} rather '
                                       f'than gone, on {branch}; waking it continues '
                                       f'the same worktree, branch and pull request'],
                           f'mrmap-controller recover {issue} --confirm', sess.id)
        return verdict(ORPHANED, [f'session {sess.id} is terminated and nothing else '
                                  f'carries this issue; its workspace may hold '
                                  f'unpushed work the controller cannot see'],
                       release, sess.id)

    # Nothing live: the pull request is the fact that matters.
    prs = _open_pulls(ctrl, issue, evidence)
    if len(prs) > 1:
        return verdict(AMBIGUOUS, [f'{len(prs)} open pull requests reference this '
                                   f'issue ({", ".join("#" + str(p.get("number")) for p in prs)}); '
                                   f'#1085 had #1123 and #1129, and closing one is '
                                   f'a human decision about which delivered the work'],
                       'a human closes or merges one, then: ' + release)
    if prs:
        kind, action = _pr_state(ctrl, prs[0], evidence)
        return verdict(kind, action=action + release)
    merged = _merged_reference(ctrl, issue, row, evidence)
    if merged:
        return verdict(COMPLETED, merged,
                       f'a human closes #{issue} if the merged pull request delivered '
                       f'the work, otherwise: ' + release)
    dead = [s for s in ctrl.by_issue.get(issue, []) if s.is_terminated]
    if claim['state'] in (db.CLAIM_SPAWNING, db.CLAIM_QUEUED) and \
            ctrl.within_grace(claim['created_at']):
        return verdict(PENDING, ['claim was taken before a spawn that AO has not '
                                 'confirmed yet, and it is still inside the grace '
                                 'window; acting here is exactly the double-start '
                                 'the audit was about'],
                       'wait one more tick')
    asked = db.events_for_issue(ctrl.db, issue,
                                (KIND_DECISION_REQUEST, KIND_DECISION_REQUEST_OBSERVED))
    if asked:
        return verdict(WAITING_HUMAN, [
            f'{len(asked)} human decision(s) were recorded as asked on this issue and '
            f'never answered; the work is parked on the answer, which is not a fault',
            f'decision sources: {", ".join(str(s) for s in asked)}'],
            'a human answers on the issue or its epic; the next tick wakes whoever '
            'asked, and nothing here may wake it first')
    if dead:
        gone = dead[0]
        detail = (f'session {gone.id} is terminated'
                  + (f' and reports workspace {gone.workspace_cleanup}'
                     if gone.workspace_cleanup else ' with no workspace report')
                  + f'; it was on {gone.branch or "a branch AO named itself"}')
        if gone.workspace_cleanup == 'removed':
            detail += (': the workspace is gone, so nothing is left to rescue and no '
                       'unpushed commit can still be recovered from it')
        else:
            detail += (': its worktree may hold commits nobody pushed, and the '
                       'controller neither sees nor deletes worktrees')
        return verdict(ORPHANED, [detail], release, gone.id)
    return verdict(ORPHANED, ['no claim-carrying session and no open pull request: '
                              'the work never started, or died before it pushed '
                              'anything'], release)


def _open_pulls(ctrl, issue, evidence):
    """Open pull requests that reference this issue under the W6 rule."""
    if not ctrl.gh_ok:
        return []
    prs = [p for p in ctrl.open_prs if ctrl.gh.pr_references_issue(p, issue)]
    if prs:
        evidence.append('open pull requests: ' + ', '.join(
            f'#{p.get("number")} on {((p.get("head") or {}).get("ref") or p.get("headRefName") or "?")}'
            for p in prs))
    return prs


def _pr_state(ctrl, pr, evidence):
    """What one open pull request is waiting for, as (kind, action words)."""
    number = pr.get('number')
    state = (pr.get('state') or 'OPEN').upper()
    decision = (pr.get('reviewDecision') or '').upper()
    mergeable = (pr.get('mergeable') or '').upper()
    rollup = pr.get('statusCheckRollup')
    if rollup is None:                       # the open-pulls query may omit it
        try:
            rollup = (ctrl.gh.pr_view(number) or {}).get('statusCheckRollup')
        except GithubError as exc:
            evidence.append(f'PR #{number}: checks unreadable ({exc})')
    ci, _ = interpret_ci(rollup, expected_sha=pr.get('headRefOid'))
    evidence.append(f'PR #{number} ({state}): CI {ci}, review {decision or "none"}, '
                    f'mergeable {mergeable or "unknown"}')
    if decision == 'APPROVED' and ci == 'success' and mergeable == 'MERGEABLE':
        return READY, (f'a human merges #{number}; the controller never merges, and '
                       f'then: ')
    return AWAITING_PR, (f'the follow loop keeps working #{number}; the quarantined '
                         f'claim can go once the pull request is the only fact left: ')


def _merged_reference(ctrl, issue, row, evidence):
    """Evidence that merged work already covers this issue, or an empty list.

    Two sources, in order of trustworthiness: the pull request the controller
    recorded for the issue, and GitHub's own cross-reference timeline. Neither
    one is allowed to become a conclusion about *delivery* -- only closing
    linkage proves that, and that is W2's command, not this one.
    """
    candidates = []
    if row and row['pr_number']:
        candidates.append(('recorded on this issue', row['pr_number']))
    for ref in ctrl.gh.cross_referenced_pulls(issue):
        if ref.get('number') not in [n for _, n in candidates]:
            candidates.append(('cross-referenced on the issue', ref['number']))
    found = []
    for origin, number in candidates:
        try:
            view = ctrl.gh.pr_view(number)
        except GithubError as exc:
            evidence.append(f'PR #{number} ({origin}): unreadable ({exc})')
            continue
        state = (view.get('state') or '').upper()
        merged_at = view.get('mergedAt')
        head = ((view.get('head') or {}).get('ref') or view.get('headRefName') or '?')
        evidence.append(f'PR #{number} ({origin}): {state}'
                        + (f', merged {merged_at}' if merged_at else '')
                        + f', branch {head}')
        if state == 'MERGED':
            found.append(f'#{number}')
    if not found:
        return []
    return [f'merged pull request(s) {" and ".join(found)} point at this issue; the '
            f'issue is still open, which is the #981/#995 pattern']


def assess_all(ctrl, issues=None, include_merged=False):
    """Verdicts for the given issues, or for every work item that is not at rest.

    A ``merged`` claim is history, not a stuck item, and listing it buries the
    eight quarantines under sixty closed issues -- with one exception the reader
    should not lose: a merged claim whose issue is still open is the #981
    pattern, and :command:`reconcile-closing` (W2) owns exactly that. Pass
    ``include_merged`` to see everything anyway.
    """
    if issues is not None:
        return [assess(ctrl, int(issue)) for issue in issues]
    selected = []
    for claim in db.all_claims(ctrl.db):
        if claim['state'] == db.CLAIM_RELEASED:
            continue
        if claim['state'] == db.CLAIM_MERGED and not include_merged:
            continue
        selected.append(claim['issue'])
    return [assess(ctrl, int(issue)) for issue in sorted(selected)]


def recover(ctrl, issue, confirm=False):
    """Wake a sleeping worker on an ownership check. Everything else is refused.

    This is the only action a verdict authorises, and it is deliberately the
    smallest one: the session already exists, already owns the branch, the
    worktree and the pull request, and is merely not running. No session is
    created, no worktree is touched, nothing is deleted, and a terminated
    session stays terminated -- its workspace may hold commits nobody pushed,
    and that is a human's call.
    """
    verdict = assess(ctrl, issue)
    if verdict.kind != RECOVERABLE:
        return False, _refused(verdict)
    if not confirm:
        return False, (f'#{issue} is resumable (session {verdict.session_id}); '
                       f're-run with --confirm to wake it. Nothing has been done.')
    claim = db.get_claim(ctrl.db, issue)
    sessions = ctrl.by_issue.get(issue, [])
    live = [s for s in sessions if not s.is_terminated]
    # The ownership check the issue asks for, in one place: exactly one session,
    # the one the claim names, no other worker executing this issue's work.
    if len(live) != 1:
        return False, (f'refused: {len(live)} live sessions for #{issue}; recovery '
                       f'needs exactly one, and will not choose')
    sess = live[0]
    if claim['session_id'] not in (None, sess.id):
        return False, (f'refused: the claim records session {claim["session_id"]}, '
                       f'not {sess.id}; someone else owns this claim')
    if not sess.resumable:
        return False, (f'refused: session {sess.id} is {sess.activity!r}, not an '
                       f'exited agent that can be woken')
    if not ctrl.worker_capacity().free():
        return False, (f'refused: {ctrl.worker_capacity().summary()}; waking it would '
                       f'exceed the worker cap')
    ok, contradiction = identity.verify_assignment(issue, sess.branch)
    if not ok:
        return False, f'refused: {contradiction}'
    ctrl.ao.resume_agent(sess.id)
    if ctrl.execute:
        db.set_claim(ctrl.db, issue, db.CLAIM_SPAWNED, ctrl.now(),
                     note=f'resumed session {sess.id} after '
                          f'{claim["state"]}; operator confirmed',
                     session_id=sess.id)
        ctrl.project(issue, st.RUNNING, ctrl.now(), session_id=sess.id,
                     reason='worker woken by operator through reconcile/recover')
    return True, (f'resumed #{issue}: session {sess.id} woken'
                  + ('' if ctrl.execute else ' (dry run: claim left as it is)'))


def _refused(verdict):
    if verdict.kind == UNCERTAIN:
        return ('refused: GitHub or AO could not be read, and a recovery on '
                'uncertain facts is how a second worker appears')
    return (f'refused: #{verdict.issue} is {verdict.kind}, not {RECOVERABLE}. '
            + (f'What a human can do: {verdict.action}' if verdict.action
               else 'No action is available; the evidence is above.'))


def report(verdicts, explain=False):
    """The printed report: grouped by kind, so a long quarantine list scans."""
    out = [f'{len(verdicts)} work item(s) examined']
    if explain:
        for verdict in verdicts:
            out.append(verdict.line(explain=True))
        return '\n'.join(out)
    for kind in ALL_KINDS:
        group = [v for v in verdicts if v.kind == kind]
        if not group:
            continue
        out.append(f'{kind} ({len(group)}):')
        for verdict in group:
            out.append(f'  #{verdict.issue}'
                       + (f'  -> {verdict.action}' if verdict.action else ''))
    return '\n'.join(out)
