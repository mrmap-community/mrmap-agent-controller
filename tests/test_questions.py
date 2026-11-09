"""W3: the question & answer lifecycle.

Fixtures are the verbatim GitHub payloads of tests/fixtures_w3.py: the
decision-request ``dr-85921e8f`` and jokiefer's answer on epic #1175, the
``agent-question`` ``0b014d9f`` on #831. All offline -- gh and ao are the
doubles; nothing is ever posted or sent anywhere for real.
"""
import json
import unittest

from mrmap_controller import db
from mrmap_controller import questions
from tests import fakes, fixtures_w3
from tests.fakes import Harness, session

EPIC, CHILD, WORKER = 1175, 1177, 'mrmap-80'
QUESTION_ISSUE, QUESTION_WORKER = 831, 'mrmap-70'
ANSWER_ID = 6731420013            # the stored 'Q1: B / Q2: A / Q3: B' comment
AUDIT_ID = 6735810252             # the bot's decision-audit comment
ANSWER_TEXT = 'Q1: B\nQ2: A\nQ3: B'
DR = 'dr-85921e8f'


def decision_comments(answers=(), drop=()):
    return fixtures_w3.rest_comments(EPIC, answers=answers, drop=drop)


def agent_question_comments(answers=()):
    return fixtures_w3.rest_comments(QUESTION_ISSUE, answers=answers)


class QuestionHarness(Harness):
    """Harness pre-wired with the epic #1175 / child #1177 scenario.

    The child issue carries a spawned claim held by ``mrmap-80`` -- the
    worker the audit says must be woken by the answer on the epic. Comments
    served for one issue (``comments_for``) default to the epic.
    """

    def __init__(self, comments, issues=None, parents=None, workers=None,
                 questions_enabled='send', execute=True, claim_child=True,
                 comments_for=EPIC):
        comments_map = {EPIC: [], CHILD: [], QUESTION_ISSUE: []}
        comments_map[comments_for] = comments
        gh = fakes.FakeGitHub(
            issues=issues if issues is not None
            else [{'number': CHILD, 'title': 'child work', 'state': 'open',
                   'labels': []}],
            parents=parents if parents is not None else {CHILD: EPIC},
            issue_comments=comments_map)
        super().__init__(gh=gh,
                         ao=fakes.FakeAO(sessions=workers if workers
                                         is not None else
                                         [session(WORKER, issue=CHILD,
                                                  activity='idle')]),
                         execute=execute,
                         config_overrides={'questions_enabled': questions_enabled})
        if claim_child:
            self.claim(CHILD, db.CLAIM_SPAWNED, session_id=WORKER,
                       age_minutes=120)

    def run_questions(self, execute=None):
        controller = self.controller(execute=execute)
        controller.load_facts()
        return controller.questions()

    def edited(self, comment_id, updated_at='2026-11-09T00:00:00Z'):
        """Simulate a human edit of a stored comment (same id, new time)."""
        for cs in self.gh.comments_by_number.values():
            for c in cs:
                if c['id'] == comment_id:
                    c['updated_at'] = updated_at
                    return
        raise AssertionError(f'comment {comment_id} not served')


class DecisionRequestLifecycle(unittest.TestCase):
    """dr-85921e8f: the stored answer 'Q1: B / Q2: A / Q3: B' on the epic
    wakes the child issue's claimant -- once, verbatim, with the permalink."""

    def setUp(self):
        self.h = QuestionHarness(decision_comments())

    def test_full_lifecycle_wakes_the_child_claimant_once_with_the_answer(self):
        self.h.run_questions()
        self.assertEqual(len(self.h.ao.sends), 1)
        sid, text = self.h.ao.sends[0]
        self.assertEqual(sid, WORKER)
        # the answer verbatim, as quote lines, and its permalink
        self.assertIn('> Q1: B', text)
        self.assertIn('> Q2: A', text)
        self.assertIn('> Q3: B', text)
        self.assertIn(f'#issuecomment-{ANSWER_ID}', text)
        self.assertIn('scripts/wip answers && scripts/wip claim', text)
        self.assertIn(DR, text)
        self.assertIn('jokiefer', text)          # answerer is named
        # consumed under the protocol's own ids, so a second tick is silent
        self.assertTrue(db.seen(self.h.conn, 'decision_request', DR))
        self.assertTrue(db.seen(self.h.conn, 'decision_child_wake',
                                f'{DR}:{CHILD}'))
        self.h.run_questions()
        self.assertEqual(len(self.h.ao.sends), 1)
        # the child claim itself is untouched: a wake is not a state change
        self.assertEqual(db.get_claim(self.h.conn, CHILD)['state'],
                         db.CLAIM_SPAWNED)

    def test_answer_is_verified_against_author_association(self):
        # the same text from a CONTRIBUTOR answers nothing: one
        # QUESTION_UNVERIFIED line per reply and the request stays open
        self.h = QuestionHarness(decision_comments(
            answers=[(ANSWER_TEXT, 'passerby', 'CONTRIBUTOR')],
            drop=[ANSWER_ID]))
        self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])
        self.assertFalse(db.seen(self.h.conn, 'decision_request', DR))
        unverified = self.h.log.events('QUESTION_UNVERIFIED')
        self.assertEqual(len(unverified), 1)
        self.assertIn('passerby', self.h.log.reasons('QUESTION_UNVERIFIED'))

    def test_answer_from_bot_is_never_verified(self):
        # mrmap-agent-bot posts the right text and is a MEMBER -- it is
        # still a Bot and its account is on ignore_authors: no answer
        self.h = QuestionHarness(decision_comments(
            answers=[(ANSWER_TEXT, 'mrmap-agent-bot', 'MEMBER')],
            drop=[ANSWER_ID]))
        self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])
        self.assertFalse(db.seen(self.h.conn, 'decision_request', DR))

    def test_machine_comments_never_answer(self):
        # the stored decision-audit comment of the bot is excluded from the
        # replies: the request reads as unanswered, and silence records
        # nothing -- no wake, no consumption, gap logged
        self.h = QuestionHarness(decision_comments(drop=[ANSWER_ID]))
        self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])
        self.assertEqual(self.h.gh.posted_comments, [])
        self.assertFalse(db.seen(self.h.conn, 'decision_request', DR))
        self.assertIn(DR, self.h.log.reasons('QUESTION_GAP'))

    def test_partial_answer_logs_the_gap_and_wakes_nobody(self):
        # Q1 answered but Q2/Q3 missing: nothing is guessed, no child wakes
        self.h = QuestionHarness(decision_comments(
            answers=[('Q1: B', 'jokiefer', 'MEMBER')], drop=[ANSWER_ID]))
        self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])
        self.assertFalse(db.seen(self.h.conn, 'decision_request', DR))
        reasons = self.h.log.reasons('QUESTION_GAP')
        self.assertIn('Q2', reasons)
        self.assertIn('Q3', reasons)

    def test_ignored_author_cannot_answer(self):
        self.h.cfg._v['ignore_authors'] = ['jokiefer']
        self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])
        self.assertFalse(db.seen(self.h.conn, 'decision_request', DR))

    def test_edited_answer_acts_once_per_stable_id(self):
        # an edit is a new updated_at but the same comment id; the request
        # id (not the comment) keys the consumption, so one wake total
        self.h.run_questions()
        self.assertEqual(len(self.h.ao.sends), 1)
        self.h.edited(ANSWER_ID)
        self.h.run_questions()
        self.assertEqual(len(self.h.ao.sends), 1)

    def test_epic_answer_wakes_claimed_children_but_not_the_epic_claim(self):
        self.h = QuestionHarness(
            decision_comments(),
            workers=[session(WORKER, issue=CHILD, activity='idle'),
                     session('mrmap-79', issue=EPIC, activity='idle')])
        self.h.claim(EPIC, db.CLAIM_SPAWNED, session_id='mrmap-79',
                     age_minutes=300)
        self.h.run_questions()
        self.assertEqual(len(self.h.ao.sends), 1)
        self.assertEqual(self.h.ao.sends[0][0], WORKER)   # never mrmap-79

    def test_session_carried_child_of_the_epic_is_woken_too(self):
        # a live worker on a child issue the controller never claimed (a
        # foreign claimant like #831/mrmap-70) is a wake candidate as well
        self.h = QuestionHarness(
            decision_comments(),
            issues=[{'number': CHILD, 'title': 'child work', 'state': 'open',
                     'labels': []},
                    {'number': CHILD + 1, 'title': 'other child',
                     'state': 'open', 'labels': []}],
            parents={CHILD: EPIC, CHILD + 1: EPIC},
            workers=[session(WORKER, issue=CHILD, activity='idle'),
                     session('mrmap-88', issue=CHILD + 1, activity='idle')],
            claim_child=False)
        self.h.run_questions()
        self.assertEqual({s for s, _ in self.h.ao.sends},
                         {WORKER, 'mrmap-88'})

    def test_terminated_claimant_is_never_resurrected(self):
        # the answer is verified and the worker is gone: nothing is resumed
        # or sent; the board gets one comment and the question is consumed
        self.h = QuestionHarness(
            decision_comments(),
            workers=[session(WORKER, issue=CHILD, activity='idle',
                             terminated=True)])
        self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])
        self.assertEqual(self.h.ao.resumes, [])
        self.assertTrue(any(e[1] == 'QUESTION_NO_WORKER'
                            for e in self.h.log.records))
        self.assertEqual(len(self.h.gh.posted_comments), 1)
        number, body = self.h.gh.posted_comments[0]
        self.assertEqual(number, self.h.cfg.board_issue)
        self.assertIn(DR, body)
        self.assertIn('does not restore terminated sessions', body)
        self.assertTrue(db.seen(self.h.conn, 'decision_request', DR))

    def test_adopted_reconcile_claim_stays_put(self):
        # needs_reconciliation is never woken into and never rewritten here
        db.set_claim(self.h.conn, CHILD, db.CLAIM_RECONCILE, fakes.now(-200))
        self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])
        self.assertEqual(db.get_claim(self.h.conn, CHILD)['state'],
                         db.CLAIM_RECONCILE)

    def test_released_claim_is_not_resurrected_though_live_session_wakes(self):
        # releasing a claim gives up the board reservation, not the worker:
        # a session still live on the issue is woken, but the claim row
        # stays released -- the wake tells the worker to re-claim itself
        db.set_claim(self.h.conn, CHILD, db.CLAIM_RELEASED, fakes.now(-200))
        self.h.run_questions()
        self.assertEqual([s for s, _ in self.h.ao.sends], [WORKER])
        self.assertTrue(db.seen(self.h.conn, 'decision_request', DR))
        self.assertEqual(db.get_claim(self.h.conn, CHILD)['state'],
                         db.CLAIM_RELEASED)
        self.assertEqual(self.h.ao.resumes, [])

    def test_request_answered_but_no_claimed_child_consumes_without_wake(self):
        # the request lives on an issue with no claimed children of its own
        self.h = QuestionHarness(
            decision_comments(), parents={CHILD: None}, comments_for=CHILD,
            claim_child=False)
        self.h.claim(CHILD, db.CLAIM_SPAWNED, session_id=WORKER,
                     age_minutes=60)
        summary = self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])
        self.assertTrue(db.seen(self.h.conn, 'decision_request', DR))
        self.assertTrue(any(e[1] == 'decision_request_answered_no_child'
                            for e in self.h.log.records))
        self.assertEqual(summary['mode'], 'send')

    def test_report_only_records_then_send_does_not_replay(self):
        # first pass in report-only: the decision is logged and consumed
        self.h.cfg._v['questions_enabled'] = 'report-only'
        self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])
        self.assertTrue(self.h.log.events('question_would_wake'))
        self.assertTrue(db.seen(self.h.conn, 'decision_request', DR))
        # flipping the switch later must not replay the old answer
        self.h.cfg._v['questions_enabled'] = 'send'
        self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])

    def test_dry_run_previews_without_consuming(self):
        # send mode, but the tick does not execute: a preview line only,
        # and the next executing tick still sees the answer
        self.h.run_questions(execute=False)
        self.assertEqual(self.h.ao.sends, [])
        self.assertEqual(self.h.gh.posted_comments, [])
        self.assertTrue(self.h.log.events('question_would_wake'))
        self.assertFalse(db.seen(self.h.conn, 'decision_request', DR))

    def test_off_mode_does_not_poll_at_all(self):
        self.h = QuestionHarness(decision_comments(), questions_enabled='off')
        self.h.run_questions()
        self.assertEqual(self.h.ao.sends, [])
        self.assertEqual(self.h.gh.names(), ['open_pulls'])   # load_facts only

    def test_unreadable_comments_do_not_eat_the_tick(self):
        self.h.gh.fail_on.add('issue_comments')
        summary = self.h.run_questions()
        self.assertIsNotNone(summary)          # the stage survived
        self.assertEqual(self.h.ao.sends, [])
        self.assertTrue(self.h.log.events('question_comments_unreadable'))


class AgentQuestionLifecycle(unittest.TestCase):
    """The other protocol: scripts/wip's agent-question on the worker's own
    issue (#831's payload verbatim; it had no human answer when fetched, so
    tests append synthetic replies the way GitHub would serve them)."""

    ANSWER = 'Test 2 is the same as Test 1'

    def harness(self, answers=(), workers=None, questions_enabled='send'):
        return QuestionHarness(
            agent_question_comments(answers=answers),
            issues=[{'number': QUESTION_ISSUE, 'title': 'harvest',
                     'state': 'open', 'labels': []}],
            parents={QUESTION_ISSUE: None},
            workers=workers if workers is not None else
            [session(QUESTION_WORKER, issue=QUESTION_ISSUE, activity='idle')],
            questions_enabled=questions_enabled,
            claim_child=False, comments_for=QUESTION_ISSUE)

    def with_claim(self, h):
        # #831's claim holder (519ca074bf12:mrmap-70) is adopted, not spawned
        h.claim(QUESTION_ISSUE, db.CLAIM_ADOPTED, session_id=QUESTION_WORKER,
                age_minutes=200)
        return h

    def test_answer_wakes_the_claimant_with_verbatim_answer_and_permalink(self):
        h = self.with_claim(self.harness(
            answers=[(self.ANSWER, 'jokiefer', 'MEMBER')]))
        h.run_questions()
        self.assertEqual(len(h.ao.sends), 1)
        sid, text = h.ao.sends[0]
        self.assertEqual(sid, QUESTION_WORKER)
        self.assertIn(f'> {self.ANSWER}', text)          # verbatim, quoted
        self.assertIn('issuecomment-', text)            # permalink to the reply
        self.assertIn('0b014d9f', text)                 # the protocol's own id
        self.assertIn('scripts/wip answers && scripts/wip claim', text)
        self.assertTrue(db.seen(h.conn, 'agent_question', '0b014d9f'))
        h.run_questions()                                # idempotent
        self.assertEqual(len(h.ao.sends), 1)

    def test_exited_but_live_claimant_is_resumed_before_sending(self):
        h = self.with_claim(self.harness(
            answers=[(self.ANSWER, 'jokiefer', 'MEMBER')],
            workers=[session(QUESTION_WORKER, issue=QUESTION_ISSUE,
                             activity='exited')]))
        h.run_questions()
        self.assertEqual(h.ao.resumes, [QUESTION_WORKER])
        self.assertEqual(len(h.ao.sends), 1)

    def test_unanswered_question_records_nothing(self):
        # the stored state itself: question open, only machine comments
        # after it -- silence is never an answer and nothing is acted on
        h = self.with_claim(self.harness())
        h.run_questions()
        self.assertEqual(h.ao.sends, [])
        self.assertEqual(h.gh.posted_comments, [])
        self.assertFalse(db.seen(h.conn, 'agent_question', '0b014d9f'))

    def test_answered_marker_set_by_wip_answers_skips_the_question(self):
        # once the worker pulled the answer, scripts/wip marks the question
        # comment; the controller then has nothing left to deliver
        h = self.with_claim(self.harness(
            answers=[(self.ANSWER, 'jokiefer', 'MEMBER')]))
        for c in h.gh.comments_by_number[QUESTION_ISSUE]:
            if 'agent-question' in c['body']:
                c['body'] = c['body'].replace(
                    '-->', ' <!-- agent-question-answered -->', 1)
        h.run_questions()
        self.assertEqual(h.ao.sends, [])

    def test_terminated_worker_is_surfaced_not_resurrected(self):
        h = self.with_claim(self.harness(
            answers=[(self.ANSWER, 'jokiefer', 'MEMBER')],
            workers=[session(QUESTION_WORKER, issue=QUESTION_ISSUE,
                             activity='idle', terminated=True)]))
        h.run_questions()
        self.assertEqual(h.ao.sends, [])
        self.assertEqual(h.ao.resumes, [])
        posts = [p for p in h.gh.posted_comments
                 if 'waiting worker is gone' in p[1]]
        self.assertEqual(len(posts), 1)
        self.assertIn('0b014d9f', posts[0][1])
        # surfaced means decided: the same question is not re-surfaced
        h.run_questions()
        self.assertEqual(len(h.gh.posted_comments), 1)

    def test_no_session_on_the_issue_is_surfaced_once_too(self):
        # the claim names a session that no longer exists in AO at all
        h = self.with_claim(self.harness(
            answers=[(self.ANSWER, 'jokiefer', 'MEMBER')], workers=[]))
        h.run_questions()
        self.assertEqual(h.ao.sends, [])
        posts = [p for p in h.gh.posted_comments
                 if 'waiting worker is gone' in p[1]]
        self.assertEqual(len(posts), 1)
        self.assertIn('no waiting session carries this issue',
                      h.log.reasons('QUESTION_NO_WORKER'))

    def test_unverified_answer_gets_no_wake(self):
        h = self.with_claim(self.harness(
            answers=[(self.ANSWER, 'someone', 'NONE')]))
        h.run_questions()
        self.assertEqual(h.ao.sends, [])
        self.assertFalse(db.seen(h.conn, 'agent_question', '0b014d9f'))
        self.assertIn('someone', h.log.reasons('QUESTION_UNVERIFIED'))

    def test_a_failed_wake_stays_open_for_the_next_tick(self):
        h = self.with_claim(self.harness(
            answers=[(self.ANSWER, 'jokiefer', 'MEMBER')]))
        h.ao.send_fails = True
        h.run_questions()
        self.assertFalse(db.seen(h.conn, 'agent_question', '0b014d9f'))
        self.assertTrue(h.log.events('QUESTION_WAKE_FAILED'))
        h.ao.send_fails = False
        h.run_questions()                    # retried next tick, then consumed
        self.assertEqual(len(h.ao.sends), 1)
        self.assertTrue(db.seen(h.conn, 'agent_question', '0b014d9f'))


class QuestionParsers(unittest.TestCase):
    """The recognisers speak the two protocols exactly as the repository
    tools write them -- verified against the stored payloads themselves."""

    def test_decision_request_marker_verbatim(self):
        body = fixtures_w3.request_comment(EPIC)['body']
        m = questions.REQUEST_RE.search(body)
        self.assertIsNotNone(m)
        payload = json.loads(m.group(1))
        self.assertEqual(payload['id'], DR)
        asks = payload['asks']
        self.assertEqual([a['q'] for a in asks], ['Q1', 'Q2', 'Q3'])
        self.assertEqual([a['q'] for a in asks if a.get('blocking', True)],
                         ['Q1', 'Q2', 'Q3'])

    def test_agent_question_marker_verbatim(self):
        body = fixtures_w3.question_comment(QUESTION_ISSUE)['body']
        m = questions._unquoted_match(questions.QUESTION_RE, body)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), '519ca074bf12:mrmap-70')
        self.assertEqual(m.group(2), '0b014d9f')

    def test_machine_comments_are_recognised(self):
        for raw in fixtures_w3.raw_comments(EPIC):
            as_rest = fixtures_w3.rest_comment(raw)
            if 'decision-request' in raw['body'] or 'decision-audit' in raw['body']:
                self.assertTrue(questions.is_machine_comment(as_rest))
            if str(ANSWER_ID) in raw['url']:
                self.assertFalse(questions.is_machine_comment(as_rest))
        for raw in fixtures_w3.raw_comments(QUESTION_ISSUE):
            as_rest = fixtures_w3.rest_comment(raw)
            if 'agent-claim' in raw['body'] or 'agent-board' in raw['body']:
                self.assertTrue(questions.is_machine_comment(as_rest))

    def test_quoted_and_fenced_text_never_answers(self):
        ask_ids = ['Q1', 'Q2', 'Q3']
        comment = lambda body: {'body': body}       # noqa: E731
        # a copy/paste template inside the request stays data
        self.assertEqual(questions.parse_decision_answer(
            comment('```text\nQ1: B\nQ2: A\nQ3: B\n```'), ask_ids), {})
        # a quote-reply carries the tokens as citation, not as a decision
        self.assertEqual(questions.parse_decision_answer(
            comment('> Q1: B\n> Q2: A\n> Q3: B'), ask_ids), {})
        # the CLI's own range syntax is not an answer shape
        self.assertEqual(questions.parse_decision_answer(
            comment('scripts/decisions ... Q1-Q3: accepted'), ask_ids), {})

    def test_answers_of_unasked_questions_are_ignored(self):
        got = questions.parse_decision_answer(
            {'body': 'Q1: B\nQ9: Z'}, ['Q1', 'Q2', 'Q3'])
        self.assertEqual(got, {'Q1': 'B'})

    def test_same_question_twice_differently_is_refused_not_guessed(self):
        got = questions.parse_decision_answer(
            {'body': 'Q1: B\nQ1: C'}, ['Q1'])
        self.assertEqual(got, {})
