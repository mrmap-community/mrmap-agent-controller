"""Question & answer lifecycle (W3): answered questions wake their workers.

Both machine protocols already on GitHub are recognized verbatim -- no
competing protocol and no minted identifiers, the stable id of a question is
the one the asking tool wrote into the marker:

* ``scripts/wip ask``  -- ``<!-- agent-question client=<cid> id=<qid> -->``
* ``scripts/decisions`` -- ``<!-- decision-request {"v":1,"id":"dr-...",
  "asks":[{"q":"Q1","blocking":true,...}]} -->``

Recognition, answer verification and the machine-comment exclusion are the
semantics of the two repository tools, copied to the extent the controller
needs them: markers only count on unquoted lines (a quote-reply carries the
marker text inside '>' lines and is exactly the human answer), claim/question/
board/request/audit comments never answer, an answerer must be a human account
whose ``author_association`` is one of ``review_associations``
(OWNER/MEMBER/COLLABORATOR) and who is not in ``ignore_authors``. An answer
that never arrives records nothing: silence is never interpreted, and the
decision-audit comments of ``scripts/decisions`` are read-only inputs here,
never rewritten.

Routing follows the two protocols: a ``wip`` question wakes the session
holding that issue's claim; a ``decision-request`` answer wakes every claimed
child-issue session of the epic once **all blocking asks** are answered --
the dr-85921e8f class, where the answer "Q1: B / Q2: A / Q3: B" on epic #1175
must wake the worker waiting on child issue #1177. A wake is a normal AO
delivery (resume if the agent exited, then send) telling the worker to run
``scripts/wip answers && scripts/wip claim``, carrying the verbatim answer and
its permalink. A terminated claimant is never resurrected
(``allow_restore_terminated`` stays false); the situation is logged and
surfaced with one comment on the board issue.

Behaviour is staged by ``questions_enabled``: ``off`` skips the stage,
``report-only`` (the shipped default) computes and records the wake decision
and logs it without any external effect, ``send`` performs it. Dry-run ticks
and paused controllers act report-only regardless of the setting. A report is
never a delivery: ``report-only`` records the decision under an *observed*
processed-events kind, so the answers that arrived during the report-only
rollout are still open when the operator switches to ``send`` -- they are
delivered then, once, instead of being lost. The delivery kinds are written
only by a tick that actually sent (or deliberately surfaced) the wake.

Idempotence per stable id: once an ``agent-question`` id or a
``decision-request`` id is consumed it stays consumed; edited comments are
re-read every tick but cannot act twice (dedupe rides on processed_events).
Wake routing reads the native sub-issue parent purely to find the child
claims of an epic -- issue relations still never influence scheduling.
"""
import json
import re

from . import db
from .ao import AOError
from .github import GithubError
from .logging_util import redact

# --- markers, verbatim from the repository tools -----------------------------
# scripts/wip:
CLAIM_RE = re.compile(r'<!-- agent-claim (\{.*?\}) -->')
QUESTION_RE = re.compile(r'<!-- agent-question client=(\S+) id=(\S+) -->')
BOARD_MARK = 'agent-board v1'
ANSWERED_MARK = 'agent-question-answered'
# scripts/decisions:
REQUEST_RE = re.compile(r'<!--\s*decision-request\s+(\{.*?\})\s*-->', re.DOTALL)
AUDIT_RE = re.compile(r'<!--\s*decision-audit\s+(\{.*?\})\s*-->', re.DOTALL)
NOTE_RE = re.compile(r'<!--\s*decision-note\s*-->')

# An answer segment "Q1: B" (also "Q1/Q2: A"); ranges like "Q1-Q3=..." belong
# to the tools' own CLI, not to what a human types into a comment.
ANSWER_SEGMENT_RE = re.compile(r'(?:^|(?<=[\s;]))((?:Q\d+)(?:\s*(?:/|,|\s+and\s+)\s*Q\d+)*)\s*:\s*(\S+)')

#: Claim states whose session is a wake candidate: a live (or exited-but-not
# terminated) worker holding the issue.
WAKEABLE = (db.CLAIM_SPAWNED, db.CLAIM_ADOPTED)

#: processed_events kinds; the source_id is always the protocol's own id.
#:
#: Two generations on purpose. The plain kinds mean *this decision was acted
#: on* (a wake was delivered, or deliberately not needed because no worker can
#: be woken). The ``*_observed`` kinds mean *this decision was seen and written
#: down while ``questions_enabled=report-only``* -- a report, not a delivery.
#: Keeping them apart is what makes the rollout safe to finish: with a single
#: kind, an answer that arrived during the report-only day was marked consumed
#: without anyone being woken, and switching the switch to ``send`` afterwards
#: could never replay it. Observed entries suppress duplicate report lines but
#: never block a later delivery.
KIND_AGENT_QUESTION = 'agent_question'
KIND_DECISION_REQUEST = 'decision_request'
KIND_DECISION_CHILD_WAKE = 'decision_child_wake'
KIND_AGENT_QUESTION_OBSERVED = 'agent_question_observed'
KIND_DECISION_REQUEST_OBSERVED = 'decision_request_observed'
KIND_DECISION_CHILD_WAKE_OBSERVED = 'decision_child_wake_observed'

#: delivered/observed pairs, so a stage can ask for "the kind I should write".
KINDS = {
    KIND_AGENT_QUESTION: KIND_AGENT_QUESTION_OBSERVED,
    KIND_DECISION_REQUEST: KIND_DECISION_REQUEST_OBSERVED,
    KIND_DECISION_CHILD_WAKE: KIND_DECISION_CHILD_WAKE_OBSERVED,
}


def mark_kind(mode, kind):
    """The processed-events kind this mode should write for ``kind``.

    ``report-only`` never claims to have delivered something: it records the
    decision under its observed twin, which keeps the delivery kind free for
    the tick that really sends the wake.
    """
    return KINDS[kind] if mode == 'report-only' else kind


# --------------------------------------------------------- text plumbing ----
def _unquoted_lines(body):
    """The lines that are not part of a GitHub quote block (scripts/wip)."""
    return [l for l in body.splitlines() if not l.lstrip().startswith('>')]


def _unquoted_match(rx, body):
    """First match of a marker regex on an unquoted line (scripts/wip)."""
    for line in _unquoted_lines(body):
        m = rx.search(line)
        if m:
            return m
    return None


def _scannable_lines(body):
    """Answer-scanning text with code fences and quotes blanked (scripts/
    decisions' scan_lines, simplified): a copy/paste answer template inside a
    request, or a quoted reply, stays data and never answers."""
    out, fence = [], None
    for ln in body.split('\n'):
        stripped = ln.strip()
        m = re.match(r'^(`{3,}|~{3,})', stripped)
        if m:
            if fence and stripped.startswith(fence):
                fence = None
            elif not fence:
                fence = m.group(1)
            out.append('')
            continue
        if fence or stripped.startswith('>'):
            out.append('')
            continue
        out.append(ln)
    return [ln for ln in out if ln.strip()]


def is_machine_comment(c):
    """Claim, question, board, request and audit comments are generated
    views: they never ask themselves here and never answer (scripts/wip's
    is_machine_comment plus the decision markers, per scripts/decisions)."""
    body = c.get('body') or ''
    return bool(_unquoted_match(CLAIM_RE, body) or _unquoted_match(QUESTION_RE, body)
                or _unquoted_match(REQUEST_RE, body) or _unquoted_match(AUDIT_RE, body)
                or _unquoted_match(NOTE_RE, body)
                or any(BOARD_MARK in l for l in _unquoted_lines(body)))


def _order_key(c):
    return (c.get('created_at') or '', c.get('id') if c.get('id') is not None else 0)


def _actor_problem(cfg, c):
    """Why this comment cannot carry a human decision, or None (the
    verification of scripts/decisions: not a Bot, association in
    OWNER/MEMBER/COLLABORATOR, not an ignored author)."""
    user = c.get('user') or {}
    login = user.get('login') or '?'
    if (user.get('type') or 'User') == 'Bot':
        return f'author {login} is a Bot account and is not counted'
    assoc = c.get('author_association') or 'ANY_COLLABORATOR'
    if assoc not in cfg.review_associations:
        return (f'author_association of {login} is {assoc} and is not counted; only '
                f'{"/".join(cfg.review_associations)} decide')
    if login in cfg.ignore_authors:
        return f'{login} is on ignore_authors and is not counted'
    return None


def parse_decision_answer(comment, ask_ids):
    """``Q<id> -> verbatim token`` for one comment; answers of questions the
    request never asked are ignored, and a question answered twice with
    different tokens in one comment is refused as ambiguous, never guessed."""
    found, ambiguous = {}, set()
    for line in _scannable_lines(comment.get('body') or ''):
        for m in ANSWER_SEGMENT_RE.finditer(line):
            token = m.group(2)
            for q in re.findall(r'Q\d+', m.group(1)):
                if q in found and found[q] != token:
                    ambiguous.add(q)
                elif q not in found:
                    found[q] = token
    for q in ambiguous:
        found.pop(q, None)
    return {q: tok for q, tok in found.items() if q in set(ask_ids)}


def build_wake_message(cfg, *, protocol, question_id, issue, answer):
    """The AO message that wakes a waiting worker whose question was answered."""
    quoted = '\n'.join('> ' + l for l in (answer.get('body') or '').strip().splitlines())
    permalink = answer.get('html_url') or \
        f'https://github.com/{cfg.repo}/issues/{issue}#issuecomment-{answer.get("id")}'
    parts = [
        '[MrMap Agent Controller] A verified human answer arrived for your open '
        f'question ({protocol}, id `{question_id}`) on issue #{issue}.',
        f"Answerer: @{(answer.get('user') or {}).get('login', '?')} "
        f"(author_association={answer.get('author_association')}).",
        '',
        'Run now to pull the answer and refresh your claim:',
        '    scripts/wip answers && scripts/wip claim',
        '',
        'The answer, verbatim:',
        quoted or '> (empty comment)',
        '',
        f'Permalink: {permalink}',
    ]
    text = '\n'.join(parts)
    limit = cfg.message_max_chars
    if len(text) > limit:
        text = text[:limit] + '\n[... message truncated by controller ...]'
    return text


# ------------------------------------------------------------- the stage ----
class QuestionStage:
    """One pass of the question/answer stage, run inside a tick.

    The controller instance supplies the wired parts (config, GitHub, AO,
    state DB, log, this tick's session listing) -- nothing here reaches for
    the world directly.
    """

    def __init__(self, controller):
        self.c = controller
        self.cfg = controller.cfg
        self.log = controller.log
        self.mode = self.cfg.questions_enabled
        self.parents = {}
        self.sessions_by_id = {s.id: s for s in controller.sessions}

    # ------------------------------------------------------------ helpers --
    def can_act(self):
        """Only a sending stage inside an executing, unpaused tick acts."""
        return (self.mode == 'send' and self.c.execute
                and not self.c.controller_paused)

    @property
    def decides(self):
        """Whether writing a processed-events entry for a decision is right now.

        Either the operator chose report-only -- then the decision is recorded
        as *observed*, once, so the report does not repeat every tick and a
        later ``send`` tick can still deliver it -- or the stage really acted,
        and the delivery kind closes the decision. A dry-run or paused tick
        never decides: its would-wake lines are previews, and the still-open
        question is decided for real by the next executing tick.
        """
        return self.mode == 'report-only' or self.can_act()

    def event(self, action, **fields):
        self.c.event(action, **fields)

    def parent_of(self, issue):
        if issue not in self.parents:
            self.parents[issue] = self.c.gh.issue_parent(issue)
        return self.parents[issue]

    def _polled_issues(self):
        """Claimed issues, tracked issues, the issues live worker sessions
        carry, and the parents (epics) of every wake-candidate issue -- the
        two protocols live on different issues and both must be seen (B-2: the
        answer sat on epic #1175 while the worker waited on child #1177)."""
        claimed = [claim for claim in db.all_claims(self.c.db)
                   if claim['state'] != db.CLAIM_RELEASED]
        polled = {claim['issue'] for claim in claimed}
        polled |= {row['issue'] for row in db.all_issues(self.c.db)}
        polled |= set(self.c.by_issue)
        wakeable = {claim['issue'] for claim in claimed
                    if claim['state'] in WAKEABLE and claim['session_id']}
        wakeable |= {issue for issue, sessions in self.c.by_issue.items()
                     if any(not s.is_terminated for s in sessions)}
        for issue in sorted(wakeable):
            parent = self.parent_of(issue)
            if parent is not None and parent != issue:
                polled.add(parent)
        self.claims = claimed
        return sorted(polled)

    def _comments(self, issues):
        out = {}
        for number in issues:
            try:
                out[number] = sorted(self.c.gh.issue_comments(number), key=_order_key)
            except GithubError as exc:
                self.log.warning('question_comments_unreadable', issue=number,
                                 reason=redact(exc))
        return out

    def _claimant(self, issue):
        """The session holding this issue's claim, or None.

        A claim row names its session; without one, a live AO session carrying
        the issueId (even one the controller does not manage) is the right
        worker to wake. Multiple recorded session ids on one claim is a
        contradiction, and contradictions are not guessed about.
        """
        claim = db.get_claim(self.c.db, issue)
        if claim and claim['state'] not in (db.CLAIM_RELEASED, db.CLAIM_MERGED) \
                and claim['session_id']:
            sid = claim['session_id']
            if ',' in str(sid):
                return None
            return self.sessions_by_id.get(sid)
        for sess in self.c.by_issue.get(issue, []):
            if not sess.is_terminated:
                return sess
        return None

    def _surface(self, issue, question_id, reason):
        """One comment on the board issue: a human must see this; the
        controller never restores a terminated session itself."""
        if self.can_act() and self.cfg.board_issue:
            try:
                self.c.gh.post_issue_comment(int(self.cfg.board_issue), (
                    '[mrmap-controller] A verified human answer arrived for '
                    f'question `{question_id}` on issue #{issue}, but the '
                    f'waiting worker is gone: {reason} The controller does not '
                    'restore terminated sessions -- wake or respawn the worker '
                    'so it can run `scripts/wip answers`.'))
            except GithubError as exc:
                self.log.error('question_surface_failed', issue=issue,
                               reason=redact(exc))

    def _consume(self, kind, source_id, issue, batch, pr_number=None):
        """Write down what this stage decided about one question.

        In ``report-only`` the entry is the observed twin, never the delivery
        kind: a report is not a delivery, and the tick that later sends must
        still find the decision open.
        """
        db.mark_events(self.c.db, [(mark_kind(self.mode, kind), source_id, issue,
                                    pr_number, batch)], self.c.now())

    def _already(self, kind, source_id):
        """True when this question must not be acted on again.

        A delivered decision is closed in every mode. A merely observed one
        only closes the *report*: it stops report-only from repeating the same
        line every tick, and it does not stop a later ``send`` tick from
        finally waking the worker.
        """
        if db.seen(self.c.db, kind, source_id):
            return True
        return (self.mode == 'report-only'
                and db.seen(self.c.db, KINDS[kind], source_id))

    def _wake(self, session, message):
        """Wake through the existing AO layer: resume an exited agent, send."""
        if session.resumable:
            self.c.ao.resume_agent(session.id)
        self.c.ao.send(session.id, message)

    def _decide_wake(self, kind, source_id, issue, protocol, question_id, answer,
                     session, batch_key):
        """Act on one answered question for one target session, once."""
        if session is None or session.is_terminated:
            who = 'no waiting session carries this issue' if session is None \
                else f'session {session.id} is terminated'
            if self.can_act():
                self._surface(issue, question_id,
                              f'{who} (question was answered by '
                              f'@{(answer.get("user") or {}).get("login", "?")}). ')
                self.event('QUESTION_NO_WORKER', issue=issue,
                           reason=f'answered question `{question_id}`: {who}; '
                                  f'surfaced on the board, the controller never '
                                  f'restores a terminated session')
            else:
                self.log.warning('question_answered_without_live_worker',
                                 issue=issue,
                                 detail=f'{who}; board surfacing skipped because '
                                        f'this stage does not act '
                                        f'(questions_enabled={self.mode})')
            if self.decides:
                self._consume(kind, source_id, issue, batch_key)
            return
        message = build_wake_message(self.cfg, protocol=protocol,
                                     question_id=question_id, issue=issue,
                                     answer=answer)
        if not self.can_act():
            self.log.warning('question_would_wake', issue=issue,
                             session=session.id,
                             detail=f'decision to wake {session.id} for question '
                                    f'{question_id} logged, nothing sent '
                                    f'(questions_enabled={self.mode}'
                                    + ('' if self.c.execute else ', tick is dry-run')
                                    + ')')
            if self.decides:
                self._consume(kind, source_id, issue, batch_key)
            return
        try:
            self._wake(session, message)
        except (AOError, GithubError) as exc:
            # Not marked: the same decision is retried next tick.
            self.event('QUESTION_WAKE_FAILED', issue=issue, session=session.id,
                       reason=f'wake for question `{question_id}` failed: '
                              f'{redact(exc)}')
            return
        self.event('QUESTION_ANSWERED', issue=issue, session=session.id,
                   reason=f'verified answer to {protocol} question '
                          f'`{question_id}` delivered; worker told to run '
                          f'scripts/wip answers && scripts/wip claim')
        self._consume(kind, source_id, issue, batch_key)

    # ------------------------------------------------------------- passes --
    def _agent_questions(self, comments):
        """The scripts/wip protocol: one question comment, one verified reply."""
        acted = 0
        for issue, cs in comments.items():
            for qc in cs:
                m = _unquoted_match(QUESTION_RE, qc.get('body') or '')
                if not m:
                    continue
                client, qid = m.group(1), m.group(2)
                if ANSWERED_MARK in (qc.get('body') or ''):
                    continue          # the worker already pulled it (wip answers)
                if self._already(KIND_AGENT_QUESTION, qid):
                    continue
                key = _order_key(qc)
                replies = [c for c in cs
                           if not is_machine_comment(c) and _order_key(c) > key]
                if not replies:
                    continue          # silence is never an answer
                answer = replies[0]
                problem = _actor_problem(self.cfg, answer)
                if problem:
                    self.event('QUESTION_UNVERIFIED', issue=issue,
                               reason=f'reply to question `{qid}` on #{issue} '
                                      f'is not a decision: {problem}')
                    continue
                self._decide_wake(KIND_AGENT_QUESTION, qid, issue,
                                  'scripts/wip ask', qid, answer,
                                  self._claimant(issue),
                                  f'client={client}')
                acted += 1
        return acted

    def _decision_requests(self, comments):
        """The scripts/decisions protocol: wake the epic's claimed children
        only when every blocking ask has a verified answer."""
        acted = 0
        for issue, cs in comments.items():
            for rc in cs:
                body = rc.get('body') or ''
                if not body.lstrip().startswith('<!--'):
                    continue
                m = REQUEST_RE.search(body)
                if not m:
                    continue
                try:
                    payload = json.loads(m.group(1))
                except ValueError:
                    self.log.warning('decision_request_marker_unparsable',
                                     issue=issue, detail=f'comment {rc.get("id")}')
                    continue
                dr_id = payload.get('id')
                if not dr_id:
                    self.log.warning('decision_request_without_id', issue=issue)
                    continue
                if self._already(KIND_DECISION_REQUEST, dr_id):
                    continue
                asks = [a for a in (payload.get('asks') or [])
                        if isinstance(a, dict) and a.get('q')]
                ask_ids = [a['q'] for a in asks]
                required = [a['q'] for a in asks if a.get('blocking', True)]
                key = _order_key(rc)
                answered = {}
                for ac in cs:
                    if _order_key(ac) <= key or is_machine_comment(ac):
                        continue
                    problem = _actor_problem(self.cfg, ac)
                    if problem:
                        self.event('QUESTION_UNVERIFIED', issue=issue,
                                   reason=f'reply to decision request {dr_id} is '
                                          f'not a decision: {problem}')
                        continue
                    for q, tok in parse_decision_answer(ac, ask_ids).items():
                        answered.setdefault(q, ac)
                missing = [q for q in required if q not in answered]
                if missing:
                    # The gap is a log line, not an action: nothing is woken on
                    # a partial answer and nothing is inferred from silence.
                    self.event('QUESTION_GAP', issue=issue,
                               reason=f'decision request {dr_id} still lacks '
                                      f'{", ".join(missing)}; no child is woken')
                    continue
                children = {}
                settled = {c['issue'] for c in self.claims if c['state'] in
                           (db.CLAIM_RECONCILE, db.CLAIM_MERGED, db.CLAIM_RELEASED)}
                for claim in self.claims:
                    if claim['state'] in WAKEABLE and claim['session_id'] \
                            and claim['issue'] != issue \
                            and self.parent_of(claim['issue']) == issue:
                        children[claim['issue']] = claim
                for child_issue in sorted(self.c.by_issue):
                    if child_issue in children or child_issue in settled \
                            or child_issue == issue:
                        continue
                    if self.parent_of(child_issue) == issue:
                        children[child_issue] = None
                if self.decides:
                    self._consume(KIND_DECISION_REQUEST, dr_id, issue,
                                  'request-consumed' if children
                                  else 'answered-no-claimed-child')
                if not children:
                    self.log.info('decision_request_answered_no_child', issue=issue,
                                  detail=f'{dr_id} fully answered but no claimed '
                                         f'child issue of this epic exists')
                    continue
                last = max(answered.values(), key=_order_key)
                for child_issue in sorted(children):
                    source = f'{dr_id}:{child_issue}'
                    if self._already(KIND_DECISION_CHILD_WAKE, source):
                        continue
                    self._decide_wake(KIND_DECISION_CHILD_WAKE, source,
                                      child_issue, 'scripts/decisions', dr_id,
                                      last, self._claimant(child_issue),
                                      f'answered: {", ".join(answered)}')
                    acted += 1
        return acted

    def run(self):
        if self.mode == 'off':
            self.log.debug('questions_stage_skipped',
                           detail='questions_enabled=off')
            return {'mode': self.mode, 'acted': 0, 'issues': 0}
        issues = self._polled_issues()
        comments = self._comments(issues)
        acted = self._agent_questions(comments) + self._decision_requests(comments)
        return {'mode': self.mode, 'acted': acted, 'issues': len(issues)}


def run(controller):
    """One tick of the question/answer stage (W3). Returns a small summary."""
    return QuestionStage(controller).run()
