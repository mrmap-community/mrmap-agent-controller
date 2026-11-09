"""W8: what a quarantined work item is, and what may be done about it.

The eight fixtures below are the real quarantines of 2026-11: #829 #948 #981
#1009 #1032 #1078 #1085 and #1177. Each one is a different reason for a claim to
sit in ``needs_reconciliation``, and the point of the exercise is that they must
not all receive the same answer -- and that none of them may receive an answer
that acts. Everything here is offline: the doubles in tests/fakes.py and a
throwaway database.
"""
import unittest

from mrmap_controller import db, recovery
from tests import fakes
from tests.fakes import (Harness, approved_issue, label_event, open_pr_entry,
                        pr, session)

QUARANTINED = (829, 948, 981, 1009, 1032, 1078, 1085, 1177)


def gh_for(number, title='Carry on the work', events=True, closed=False, **kw):
    return fakes.FakeGitHub(
        issues=[approved_issue(number, title=title, state='closed' if closed else 'open')],
        events=({number: [label_event('labeled', 'jokiefer')]} if events else {}),
        **kw)


class TheEightQuarantines(unittest.TestCase):
    """Each real case must land in the one kind that describes it."""

    def harness(self, number, sessions=(), pulls=(), prs=None, cross_refs=None,
                state=db.CLAIM_RECONCILE, age_minutes=120):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(number, state=state,
                session_id=(sessions[0]['session_id'] if sessions else None),
                age_minutes=age_minutes)
        gh = gh_for(number, pulls=list(pulls), prs=prs or {},
                    cross_refs=cross_refs or {})
        return h, gh, fakes.FakeAO([session(**s) for s in sessions])

    def kinds(self):
        """issue -> kind, for the eight, with the facts each one really had."""
        out = {}

        # Dead session, nothing pushed: the honest orphan.
        for number in (829, 948, 1032, 1078):
            h, gh, ao = self.harness(number, sessions=[dict(
                session_id=f'mrmap-{number}', issue=number, terminated=True)])
            out[number] = recovery.assess(h.reader(gh=gh, ao=ao), number)

        # #981: absorbed by a merged PR the controller never recorded.
        h, gh, ao = self.harness(
            981, sessions=[dict(session_id='mrmap-70', issue=981, terminated=True)],
            prs={995: pr(995, issue=981, state='MERGED')},
            cross_refs={981: [{'number': 995, 'state': 'CLOSED', 'merged': True}]})
        out[981] = recovery.assess(h.reader(gh=gh, ao=ao), 981)

        # #1009: quarantined claim, live pull request waiting for review.
        h, gh, ao = self.harness(
            1009, sessions=[dict(session_id='mrmap-88', issue=1009, terminated=True)],
            pulls=[open_pr_entry(1114, issue=1009)],
            prs={1114: pr(1114, issue=1009)})
        out[1009] = recovery.assess(h.reader(gh=gh, ao=ao), 1009)

        # #1085: two pull requests for one issue.
        h, gh, ao = self.harness(
            1085, pulls=[open_pr_entry(1123, issue=1085),
                        open_pr_entry(1129, issue=1085, head='ao/issue-1085-alt')],
            prs={1123: pr(1123, issue=1085), 1129: pr(1129, issue=1085)})
        out[1085] = recovery.assess(h.reader(gh=gh, ao=ao), 1085)

        # #1177: parked on a human answer about its epic.
        h, gh, ao = self.harness(1177)
        db.mark_events(h.conn, [(recovery.KIND_DECISION_REQUEST, 'thread-9', 1177,
                                 None, 'batch')], fakes.now())
        out[1177] = recovery.assess(h.reader(gh=gh, ao=ao), 1177)
        return out

    def test_every_case_gets_its_own_kind(self):
        verdicts = self.kinds()
        expected = {829: recovery.ORPHANED, 948: recovery.ORPHANED,
                    981: recovery.COMPLETED, 1009: recovery.AWAITING_PR,
                    1032: recovery.ORPHANED, 1078: recovery.ORPHANED,
                    1085: recovery.AMBIGUOUS, 1177: recovery.WAITING_HUMAN}
        for number, kind in expected.items():
            self.assertEqual(verdicts[number].kind, kind,
                             f'#{number}: {verdicts[number].evidence}')

    def test_no_quarantine_is_explained_by_nothing_to_do(self):
        # A verdict with no kind, or a kind that is not in the published list,
        # would be the tool inventing a category on the spot.
        for number, verdict in self.kinds().items():
            self.assertIn(verdict.kind, recovery.ALL_KINDS, number)
            self.assertTrue(verdict.evidence, f'#{number} explains nothing')
            self.assertTrue(verdict.action, f'#{number} leaves the reader nowhere')

    def test_explaining_the_board_changes_nothing(self):
        # The operator runs this to find out, often more than once. It may not
        # be the thing that finally moves a claim.
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(1009, state=db.CLAIM_RECONCILE, session_id='mrmap-88')
        gh = gh_for(1009, pulls=[open_pr_entry(1114, issue=1009)],
                    prs={1114: pr(1114, issue=1009)})
        ao = fakes.FakeAO([session('mrmap-88', issue=1009, terminated=True)])
        before_claims = [dict(c) for c in db.all_claims(h.conn)]
        before_issues = [dict(i) for i in db.all_issues(h.conn)]
        for _ in range(3):
            recovery.report(recovery.assess_all(h.reader(gh=gh, ao=ao), [1009]))
        self.assertEqual([dict(c) for c in db.all_claims(h.conn)], before_claims)
        self.assertEqual([dict(i) for i in db.all_issues(h.conn)], before_issues)
        for call in ('spawn', 'send', 'resume_agent'):
            self.assertEqual(ao.count(call), 0, call)


class AbsorbedWork(unittest.TestCase):
    """#981: merged elsewhere, still open, still claimed."""

    def reader(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(981, state=db.CLAIM_RECONCILE, session_id='mrmap-70')
        gh = gh_for(981, pulls=[], prs={995: pr(995, issue=981, state='MERGED')},
                    cross_refs={981: [{'number': 995, 'state': 'CLOSED',
                                       'merged': True}]})
        ao = fakes.FakeAO([session('mrmap-70', issue=981, terminated=True)])
        return h, recovery.assess(h.reader(gh=gh, ao=ao), 981)

    def test_merged_cross_reference_is_spotted_and_named(self):
        _, verdict = self.reader()
        self.assertEqual(verdict.kind, recovery.COMPLETED)
        self.assertIn('995', ' '.join(verdict.evidence))

    def test_absorbed_work_is_never_resumed_or_resent(self):
        # Spawning for absorbed work is how the same feature gets built twice.
        h, verdict = self.reader()
        done, message = recovery.recover(h.reader(), 981, confirm=True)
        self.assertFalse(done)
        self.assertIn(recovery.COMPLETED, message)

    def test_a_closed_issue_releases_the_claim_instead(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(981, state=db.CLAIM_RECONCILE, session_id='mrmap-70')
        gh = gh_for(981, closed=True)
        ao = fakes.FakeAO([session('mrmap-70', issue=981, terminated=True)])
        verdict = recovery.assess(h.reader(gh=gh, ao=ao), 981)
        self.assertEqual(verdict.kind, recovery.CLOSED)
        self.assertIn('release 981 --confirm', verdict.action)


class AmbiguousCases(unittest.TestCase):
    """Where two readings exist, the tool names both and chooses neither."""

    def test_two_open_pull_requests_are_never_reduced_to_one(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(1085, state=db.CLAIM_RECONCILE)
        gh = gh_for(1085, pulls=[open_pr_entry(1123, issue=1085),
                                 open_pr_entry(1129, issue=1085,
                                               head='ao/issue-1085-alt')],
                    prs={1123: pr(1123, issue=1085), 1129: pr(1129, issue=1085)})
        verdict = recovery.assess(h.reader(gh=gh, ao=fakes.FakeAO([])), 1085)
        evidence = ' '.join(verdict.evidence)
        self.assertEqual(verdict.kind, recovery.AMBIGUOUS)
        self.assertIn('#1123', evidence)
        self.assertIn('#1129', evidence)
        # The action sends the choice to a human; it must not name a winner.
        self.assertIn('human', verdict.action)
        self.assertNotIn('1123', verdict.action)
        self.assertNotIn('1129', verdict.action)

    def test_a_contradicting_branch_is_ambiguous_not_resumed(self):
        # W6 and W8 meet here: a resumable session on somebody else's branch is
        # not a wake-up call, it is a question.
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state=db.CLAIM_RECONCILE, session_id='mrmap-40')
        gh = gh_for(120)
        ao = fakes.FakeAO([session('mrmap-40', issue=120, activity='exited',
                                   branch='ao/issue-999-other-work')])
        verdict = recovery.assess(h.reader(gh=gh, ao=ao), 120)
        self.assertEqual(verdict.kind, recovery.AMBIGUOUS)
        done, message = recovery.recover(h.reader(gh=gh, ao=ao), 120, confirm=True)
        self.assertFalse(done)

    def test_uncertain_facts_explain_themselves_as_uncertain(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(829, state=db.CLAIM_RECONCILE, session_id='mrmap-11')
        gh = gh_for(829)
        gh.fail_on = {'open_pulls'}
        ao = fakes.FakeAO([session('mrmap-11', issue=829, terminated=True)])
        reader = h.reader(gh=gh, ao=ao)
        self.assertFalse(reader.gh_ok)
        verdict = recovery.assess(reader, 829)
        self.assertEqual(verdict.kind, recovery.UNCERTAIN)
        done, message = recovery.recover(reader, 829, confirm=True)
        self.assertFalse(done)
        self.assertIn('uncertain', message.lower())


class SafelyResumable(unittest.TestCase):
    """The one class the controller may act on, and its refusals."""

    def setup(self, sessions=None, capacity_free=True, claim_state=db.CLAIM_RECONCILE,
             claim_session='mrmap-40'):
        h = Harness(execute=True, config_overrides={'max_workers': 1})
        self.addCleanup(h.close)
        h.claim(120, state=claim_state, session_id=claim_session)
        gh = gh_for(120)
        listed = list(sessions) if sessions is not None else [
            session('mrmap-40', issue=120, activity='exited',
                    branch='ao/issue-120-the-thing')]
        if not capacity_free:
            # A second, executing worker on another claimed issue: the cap is
            # about the host, not about this issue.
            listed.append(session('mrmap-99', issue=131, activity='active'))
            h.claim(131, state=db.CLAIM_SPAWNED, session_id='mrmap-99')
        return h, gh, fakes.FakeAO(listed)

    def verdict(self, **kw):
        h, gh, ao = self.setup(**kw)
        return h, recovery.assess(h.reader(gh=gh, ao=ao), 120)

    def test_an_exited_agent_on_its_own_branch_is_resumable(self):
        _, verdict = self.verdict()
        self.assertEqual(verdict.kind, recovery.RESUMABLE)
        self.assertIn('recover 120 --confirm', verdict.action)

    def test_without_confirm_nothing_is_woken(self):
        h, gh, ao = self.setup()
        done, message = recovery.recover(h.reader(gh=gh, ao=ao), 120, confirm=False)
        self.assertFalse(done)
        self.assertIn('--confirm', message)
        self.assertEqual(ao.count('resume_agent'), 0)

    def test_confirm_wakes_the_one_session_and_rearms_the_claim(self):
        h, gh, ao = self.setup()
        reader = h.reader(gh=gh, ao=ao, execute=True)
        done, message = recovery.recover(reader, 120, confirm=True)
        self.assertTrue(done, message)
        self.assertEqual(ao.count('resume_agent'), 1)
        self.assertEqual(ao.with_name('resume_agent')[0]['session'], 'mrmap-40')
        self.assertEqual(db.get_claim(h.conn, 120)['state'], db.CLAIM_SPAWNED)
        self.assertEqual(h.rows()[120]['state'], 'running')

    def test_a_full_capacity_slot_refuses_the_wake(self):
        # Waking is what starts consuming capacity, so it is charged like a
        # start: the cap that protects the host does not bend for a resume.
        h, gh, ao = self.setup(capacity_free=False)
        verdict = recovery.assess(h.reader(gh=gh, ao=ao), 120)
        self.assertEqual(verdict.kind, recovery.RESUMABLE)
        self.assertIn('slot', verdict.action)
        done, message = recovery.recover(h.reader(gh=gh, ao=ao), 120, confirm=True)
        self.assertFalse(done)
        self.assertIn('worker cap', message)
        self.assertEqual(ao.count('resume_agent'), 0)

    def test_a_claim_naming_another_session_is_not_touched(self):
        h, gh, ao = self.setup(claim_session='mrmap-different')
        done, message = recovery.recover(h.reader(gh=gh, ao=ao), 120, confirm=True)
        self.assertFalse(done)
        self.assertIn('owns this claim', message)
        self.assertEqual(ao.count('resume_agent'), 0)

    def test_a_second_live_session_refuses_the_wake(self):
        h, gh, ao = self.setup(sessions=[
            session('mrmap-40', issue=120, activity='exited'),
            session('mrmap-41', issue=120, activity='active')])
        verdict = recovery.assess(h.reader(gh=gh, ao=ao), 120)
        self.assertEqual(verdict.kind, recovery.AMBIGUOUS)
        done, message = recovery.recover(h.reader(gh=gh, ao=ao), 120, confirm=True)
        self.assertFalse(done)
        self.assertEqual(ao.count('resume_agent'), 0)

    def test_a_removed_workspace_says_so_because_nothing_can_be_rescued(self):
        # The difference matters to the human deciding: a live worktree might
        # still hold a commit, a removed one provably holds nothing.
        h, gh, ao = self.setup(sessions=[session('mrmap-40', issue=120,
                                                 terminated=True)])
        ao.sessions_list[0].raw['workspaceCleanup'] = 'removed'
        ao.sessions_list[0].workspace_cleanup = 'removed'
        verdict = recovery.assess(h.reader(gh=gh, ao=ao), 120)
        self.assertEqual(verdict.kind, recovery.ORPHANED)
        self.assertIn('gone, so nothing is left to rescue',
                      ' '.join(verdict.evidence))

    def test_a_live_worker_is_not_recovered_because_it_is_already_working(self):
        h, gh, ao = self.setup(sessions=[session('mrmap-40', issue=120,
                                                 activity='active')])
        verdict = recovery.assess(h.reader(gh=gh, ao=ao), 120)
        self.assertEqual(verdict.kind, recovery.LIVE)
        done, message = recovery.recover(h.reader(gh=gh, ao=ao), 120, confirm=True)
        self.assertFalse(done)

    def test_a_terminated_session_stays_terminated(self):
        # Its worktree may hold commits nobody pushed. Recovery would have to
        # guess whether that work exists; only a human can look.
        h, gh, ao = self.setup(sessions=[session('mrmap-40', issue=120,
                                                 terminated=True)])
        verdict = recovery.assess(h.reader(gh=gh, ao=ao), 120)
        self.assertEqual(verdict.kind, recovery.ORPHANED)
        self.assertIn('commits nobody pushed', ' '.join(verdict.evidence))
        done, message = recovery.recover(h.reader(gh=gh, ao=ao), 120, confirm=True)
        self.assertFalse(done)
        self.assertEqual(ao.count('resume_agent'), 0)


class SpawnThatNeverLanded(unittest.TestCase):
    """The restart-between-claim-and-spawn case: too young to conclude anything."""

    def reader(self, age_minutes):
        h = Harness(execute=True,
                    config_overrides={'spawn_reconcile_grace_minutes': 20})
        self.addCleanup(h.close)
        h.claim(120, state=db.CLAIM_SPAWNING, session_id=None,
                age_minutes=age_minutes)
        return h, recovery.assess(h.reader(gh=gh_for(120), ao=fakes.FakeAO([])), 120)

    def test_inside_the_grace_window_it_is_pending_not_orphaned(self):
        _, verdict = self.reader(age_minutes=3)
        self.assertEqual(verdict.kind, recovery.PENDING)
        self.assertIn('double-start', ' '.join(verdict.evidence))

    def test_past_the_grace_window_it_is_orphaned_with_a_release_action(self):
        _, verdict = self.reader(age_minutes=90)
        self.assertEqual(verdict.kind, recovery.ORPHANED)
        self.assertIn('release 120 --confirm', verdict.action)


class ApprovalAndPause(unittest.TestCase):

    def test_an_unapproved_issue_is_explained_as_not_approved(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state=db.CLAIM_RECONCILE, session_id='mrmap-40')
        gh = fakes.FakeGitHub(issues=[approved_issue(120)],
                              events={120: [label_event('labeled', 'somebody-else')]})
        ao = fakes.FakeAO([session('mrmap-40', issue=120, activity='exited')])
        verdict = recovery.assess(h.reader(gh=gh, ao=ao), 120)
        self.assertEqual(verdict.kind, recovery.UNAPPROVED)
        done, message = recovery.recover(h.reader(gh=gh, ao=ao), 120, confirm=True)
        self.assertFalse(done)

    def test_paused_work_is_intended_not_stuck(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state=db.CLAIM_RECONCILE, session_id='mrmap-40')
        gh = gh_for(120)
        gh.issues = [approved_issue(120, labels=('agent: approved', 'agent: paused'))]
        ao = fakes.FakeAO([session('mrmap-40', issue=120, activity='exited')])
        verdict = recovery.assess(h.reader(gh=gh, ao=ao), 120)
        self.assertEqual(verdict.kind, recovery.UNAPPROVED)
        self.assertIn('paused', ' '.join(verdict.evidence).lower())


class ProjectionRowIsRead(unittest.TestCase):
    """The issues projection is a sqlite Row, not a dict.

    This is the case the fakes could not hide: every fake fact in the suite is a
    dict that answers ``.get()``, and the projection comes out of the database.
    An explain tool that crashes on the row it exists to explain is worse than
    no tool, so this reads it the way a real quarantine reads.
    """

    def test_the_projection_row_is_part_of_the_explanation(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(1009, state=db.CLAIM_RECONCILE, session_id='mrmap-88')
        db.upsert_issue(h.conn, 1009, 'awaiting_review', fakes.now(),
                       session_id='mrmap-88', pr_number=1114,
                       reason='claim quarantined; awaiting review kept as a fact')
        gh = gh_for(1009, pulls=[open_pr_entry(1114, issue=1009)],
                    prs={1114: pr(1114, issue=1009)})
        ao = fakes.FakeAO([session('mrmap-88', issue=1009, terminated=True)])
        verdict = recovery.assess(h.reader(gh=gh, ao=ao), 1009)
        evidence = ' '.join(verdict.evidence)
        self.assertIn('projection: awaiting_review, PR #1114', evidence)
        self.assertIn('claim quarantined', evidence)
        self.assertEqual(verdict.kind, recovery.AWAITING_PR)


class Reporting(unittest.TestCase):

    def test_the_grouped_report_is_scannable_and_the_explained_one_complete(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state=db.CLAIM_RECONCILE, session_id='mrmap-40')
        verdicts = recovery.assess_all(h.reader(gh=gh_for(120),
                                                ao=fakes.FakeAO([])), [120])
        grouped = recovery.report(verdicts)
        explained = recovery.report(verdicts, explain=True)
        self.assertIn('#120', grouped)
        self.assertIn(recovery.ORPHANED, grouped)
        for fact in verdicts[0].evidence:
            self.assertIn(fact.split('\n')[0], explained)
        self.assertIn('action:', explained)

    def test_assess_all_without_arguments_covers_every_claim_not_released(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state=db.CLAIM_RECONCILE)
        h.claim(121, state=db.CLAIM_SPAWNED, session_id='mrmap-50')
        h.claim(122, state=db.CLAIM_RELEASED)
        h.claim(123, state=db.CLAIM_MERGED, session_id='mrmap-60')
        verdicts = recovery.assess_all(h.reader(gh=gh_for(120), ao=fakes.FakeAO([])))
        self.assertEqual([v.issue for v in verdicts], [120, 121])
        everything = recovery.assess_all(h.reader(gh=gh_for(120), ao=fakes.FakeAO([])),
                                         include_merged=True)
        self.assertEqual([v.issue for v in everything], [120, 121, 123])


if __name__ == '__main__':
    unittest.main()
