"""W6: one issue number, held identical across issue, branch, session and PR.

The incident is reproducible offline and is reproduced here. AO names a
worktree branch from the *spawn prompt text*, so the prompt "Implement GitHub
issue mrmap-community/mrmap#682 ..." produced
``ao/implement-github-issue-5/root``. Anything downstream that grepped
``issue-(\\d+)`` out of that name saw a work item on issue #5 -- closed in 2021
-- and wrote a claim and a board row onto it.

Offline throughout: the doubles from tests/fakes.py, a throwaway database, no
network, no repository and no ``scripts/wip`` invocation.
"""
import unittest

from mrmap_controller import ao as ao_mod
from mrmap_controller import db, identity, lifecycle as st
from mrmap_controller.messages import build_spawn_prompt
from tests import fakes
from tests.fakes import Harness, approved_issue, label_event, open_pr_entry, session

#: The branch rule as ``scripts/wip`` states it in the repository. Kept as a
#: literal on purpose: if one of the two tools changes it, the tripwire below
#: fails here instead of the two of them quietly disagreeing in production.
WIP_PATTERN = r"(?:^|/)(?:issue[-_]?|#)(\d+)"

#: What AO really named the sessions, and the issue each was working on.
INCIDENT = {
    682: 'ao/implement-github-issue-5/root',
    948: 'ao/implement-github-issue-6/root',
    981: 'ao/implement-github-issue-7/root',
}


class BranchDeclaresAnIssue(unittest.TestCase):
    """What a branch name is allowed to mean."""

    def test_a_convention_shaped_branch_declares_its_issue(self):
        for branch, number in [('ao/issue-981-mapper', 981),
                               ('ao/issue-981', 981),
                               ('issue-831', 831),
                               ('feature/issue-831-frontend', 831),
                               ('ao/issue_682_root', 682),
                               ('ao/#948-fix', 948)]:
            self.assertEqual(identity.issue_from_branch(branch), number, branch)

    def test_a_prose_slug_declares_nothing(self):
        # The letters "issue-5" inside a word are that word, not a reference.
        for branch in list(INCIDENT.values()) + [
                'ao/polling-resource/root', 'ao/mrmap-1234/root',
                'dependabot/npm_and_yarn/frontend/axios-0.28.0',
                'main', 'fix-typo-in-issue-templates', '', None]:
            self.assertIsNone(identity.issue_from_branch(branch), branch)

    def test_the_incident_branches_declare_no_issue_at_all(self):
        # Not "the wrong issue": nothing. The number in them is a prompt echo,
        # so no issue -- 5, 6, 7 or 682 -- may be derived from them.
        for issue, branch in INCIDENT.items():
            self.assertIsNone(identity.issue_from_branch(branch), branch)

    def test_the_rule_is_the_rule_the_repository_tool_uses(self):
        self.assertEqual(identity.BRANCH_ISSUE_RE.pattern, WIP_PATTERN)


class BranchForAnIssue(unittest.TestCase):
    """The name the controller asks AO to create."""

    def test_the_number_comes_from_the_assignment(self):
        branch = identity.branch_for(682, 'Repair the polling resource watcher')
        self.assertEqual(branch, 'ao/issue-682-repair-the-polling-resource-watcher')
        self.assertEqual(identity.issue_from_branch(branch), 682)

    def test_the_same_issue_always_gets_the_same_branch(self):
        title = 'Mapper配置 sollte übersetzt werden'
        self.assertEqual(identity.branch_for(948, title),
                         identity.branch_for(948, title))

    def test_two_issues_with_one_title_get_two_branches(self):
        # The title is decoration; the number is the identity.
        self.assertNotEqual(identity.branch_for(1, 'Same title'),
                            identity.branch_for(2, 'Same title'))
        self.assertEqual(identity.issue_from_branch(identity.branch_for(1, 'Same title')), 1)
        self.assertEqual(identity.issue_from_branch(identity.branch_for(2, 'Same title')), 2)

    def test_the_name_is_usable_as_a_branch(self):
        branch = identity.branch_for(120, '   !!! --- ??? ' + 'word ' * 40)
        self.assertTrue(branch.startswith('ao/issue-120-'), branch)
        self.assertLessEqual(len(branch), len('ao/issue-120-') + identity.SLUG_MAX)
        self.assertFalse(branch.endswith('-'), branch)
        self.assertNotIn('--', branch, branch)
        self.assertTrue(all(c.isalnum() or c in '/-' for c in branch), branch)

    def test_an_empty_title_still_names_a_branch(self):
        self.assertEqual(identity.branch_for(7, ''), 'ao/issue-7-work')

    def test_something_that_is_not_an_issue_number_is_refused(self):
        for bad in (0, -1, 'abc'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                identity.branch_for(bad, 'title')


class AssignmentAgainstBranch(unittest.TestCase):
    """identity.verify_assignment: agree, stay silent, or stop."""

    def test_a_branch_that_declares_nothing_is_compatible(self):
        # Deliberate: a session opened before W6 keeps working. Compatibility
        # is with the *absence* of a statement, never with a contradicting one.
        ok, _ = identity.verify_assignment(682, 'ao/mrmap-1234/root')
        self.assertTrue(ok)
        for issue, branch in INCIDENT.items():
            ok, _ = identity.verify_assignment(issue, branch)
            self.assertTrue(ok, branch)

    def test_a_branch_that_declares_the_assigned_issue_agrees(self):
        ok, reason = identity.verify_assignment(831, 'feature/issue-831-frontend')
        self.assertTrue(ok)
        self.assertIn('831', reason)

    def test_a_branch_that_declares_another_issue_is_refused(self):
        ok, reason = identity.verify_assignment(682, 'ao/issue-5-work')
        self.assertFalse(ok)
        # The human who has to resolve it sees both numbers, not one of them.
        self.assertIn('682', reason)
        self.assertIn('5', reason)


class PullRequestReference(unittest.TestCase):
    """A pull request counts for an issue only on an explicit number or its branch."""

    reference = staticmethod(fakes.FakeGitHub.pr_references_issue)

    def test_an_incident_branch_is_not_read_as_issue_five(self):
        pr = {'title': 'Fix the polling resource watcher', 'body': '',
              'head': {'ref': INCIDENT[682]}}
        for wrong in (5, 6, 7):
            self.assertFalse(self.reference(pr, wrong), wrong)

    def test_an_explicit_number_in_the_text_still_counts(self):
        pr = {'title': 'Fix the watcher', 'body': 'Refs #682',
              'head': {'ref': INCIDENT[682]}}
        self.assertTrue(self.reference(pr, 682))

    def test_the_issue_branch_counts_as_a_reference(self):
        pr = {'title': 'Fix the watcher', 'body': '',
              'head': {'ref': 'ao/issue-682-fix-the-watcher'}}
        self.assertTrue(self.reference(pr, 682))
        self.assertFalse(self.reference(pr, 683))

    def test_a_longer_number_is_not_a_prefix_match(self):
        pr = {'title': 'Fix the watcher', 'body': '',
              'head': {'ref': 'ao/issue-6821-fix'}}
        self.assertFalse(self.reference(pr, 682))

    def test_an_unparsable_issue_matches_no_branch(self):
        # The text rule is unchanged, so "None" must degrade instead of raising.
        self.assertFalse(self.reference({'title': '', 'body': '',
                                         'head': {'ref': 'ao/issue-1-x'}}, None))


class SpawnCarriesItsBranch(unittest.TestCase):
    """The contract is passed on, not just documented."""

    def gh(self, number=120, title='Repair the polling resource watcher', pulls=None):
        return fakes.FakeGitHub(
            issues=[approved_issue(number, title=title)],
            events={number: [label_event('labeled', 'jokiefer')]},
            pulls=pulls or [])

    def test_spawn_names_the_issue_branch_for_ao(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.tick(gh=self.gh())
        spawn = h.ao.with_name('spawn')[0]
        self.assertEqual(spawn['branch'],
                         identity.branch_for(120, 'Repair the polling resource watcher'))
        # The worker is told the same name it is being started on; two names
        # would mean the prompt and the workspace tell different stories.
        self.assertIn(spawn['branch'], spawn['prompt'])
        self.assertIn('ao/issue-120', spawn['branch'])

    def test_the_branch_a_spawn_created_is_accepted_by_later_ticks(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.tick(gh=self.gh())
        session_id = h.ao.sessions_list[-1].id
        branch = h.ao.sessions_list[-1].branch
        report = h.tick(gh=self.gh())          # second tick, worker is live now
        self.assertNotIn('NEEDS_HUMAN', report['counts'],
                         f'own branch {branch} must not look like a contradiction')
        self.assertEqual(h.rows()[120]['session_id'], session_id)

    def test_a_pr_on_another_issue_branch_is_not_taken_for_this_issue(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        report = h.tick(gh=self.gh(number=5, pulls=[
            open_pr_entry(500, issue=5, head=INCIDENT[682],
                          title='Fix the polling resource watcher')]))
        self.assertNotIn('references this issue', str(report['actions']),
                         'a prompt-echo branch proves nothing about issue #5')

    def test_a_pr_on_the_issue_branch_is_taken_for_it(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        report = h.tick(gh=self.gh(pulls=[
            open_pr_entry(500, issue=120,
                          head='ao/issue-120-repair-the-polling-resource-watcher')]))
        self.assertIn('references this issue', fakes.reasons(report, 'SKIP'))
        self.assertEqual(h.ao.count('spawn'), 0)


class ContradictoryIdentityStopsTheWork(unittest.TestCase):
    """A branch naming another issue is reported, never resolved by guessing."""

    def gh(self, number=120):
        return fakes.FakeGitHub(
            issues=[approved_issue(number)],
            events={number: [label_event('labeled', 'jokiefer')]})

    def test_a_session_on_another_issue_branch_is_not_adopted(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        ao = fakes.FakeAO([session('mrmap-77', issue=120,
                                   branch='ao/issue-999-somebody-elses-work')])
        report = h.tick(gh=self.gh(), ao=ao)
        self.assertIn('NEEDS_HUMAN', report['counts'])
        self.assertIsNone(db.get_claim(h.conn, 120),
                          'no claim may be written for an ambiguous association')
        self.assertEqual(h.ao.count('spawn'), 0)

    def test_a_live_contradiction_quarantines_the_claim(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state='spawned', session_id='mrmap-77')
        ao = fakes.FakeAO([session('mrmap-77', issue=120,
                                   branch='ao/issue-999-somebody-elses-work')])
        report = h.tick(gh=self.gh(), ao=ao)
        self.assertIn('NEEDS_HUMAN', report['counts'])
        self.assertEqual(db.get_claim(h.conn, 120)['state'], db.CLAIM_RECONCILE)
        # W1 still holds: a quarantined claim never projects work in progress.
        self.assertEqual(h.rows()[120]['state'], st.FAILED)

    def test_stopping_the_same_contradiction_twice_changes_nothing_more(self):
        # A tick that stops work must be repeatable: the loop runs every five
        # minutes, so reporting the ambiguity may not pile up claims or states.
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state='spawned', session_id='mrmap-77')
        ao = fakes.FakeAO([session('mrmap-77', issue=120,
                                   branch='ao/issue-999-somebody-elses-work')])
        gh = self.gh()
        h.tick(gh=gh, ao=ao)
        second = h.tick(gh=gh, ao=ao)
        self.assertIn('NEEDS_HUMAN', second['counts'])
        self.assertEqual(db.get_claim(h.conn, 120)['state'], db.CLAIM_RECONCILE)
        self.assertEqual([r['issue'] for r in db.all_claims(h.conn)], [120])
        self.assertEqual(sorted(h.rows()), [120])

    def test_a_finished_claim_is_reported_but_not_rewritten(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state=db.CLAIM_MERGED, session_id='mrmap-77')
        db.upsert_issue(h.conn, 120, st.MERGED, fakes.now(), session_id='mrmap-77')
        ao = fakes.FakeAO([session('mrmap-77', issue=120,
                                   branch='ao/issue-999-somebody-elses-work')])
        report = h.tick(gh=self.gh(), ao=ao)
        self.assertIn('NEEDS_HUMAN', report['counts'])
        self.assertEqual(db.get_claim(h.conn, 120)['state'], db.CLAIM_MERGED)
        self.assertEqual(h.rows()[120]['state'], st.MERGED)

    def test_a_branch_that_declares_nothing_keeps_working(self):
        # The pre-W6 session naming (ao/<session>/root) must not be turned into
        # an emergency by this change: it declares nothing, so it contradicts
        # nothing.
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state='spawned', session_id='mrmap-77')
        ao = fakes.FakeAO([session('mrmap-77', issue=120, branch='ao/mrmap-77/root')])
        report = h.tick(gh=self.gh(), ao=ao)
        self.assertNotIn('NEEDS_HUMAN', report['counts'])
        self.assertEqual(db.get_claim(h.conn, 120)['state'], db.CLAIM_SPAWNED)
        self.assertIn(h.rows()[120]['state'], (st.RUNNING, st.PR_OPEN))


class SpawnBranchCapability(unittest.TestCase):
    """The contract rests on a real AO flag, so doctor asks the binary about it."""

    def test_an_ao_that_lists_the_flag_is_usable(self):
        ao = ao_mod.AO(fakes.FakeRunner(responses={
            'ao spawn --help': '  --branch string   Branch for git project sessions '
                               '(default: ao/<session-id>/root)'}), 'mrmap')
        self.assertTrue(ao.spawn_branch_option_supported())

    def test_an_ao_without_the_flag_is_reported_as_unusable(self):
        ao = ao_mod.AO(fakes.FakeRunner(responses={
            'ao spawn --help': '  --issue string   Issue reference'}), 'mrmap')
        self.assertFalse(ao.spawn_branch_option_supported())

    def test_an_unreachable_ao_raises_instead_of_lying(self):
        ao = ao_mod.AO(fakes.FakeRunner(responses={}), 'mrmap')
        with self.assertRaises(ao_mod.AOError):
            ao.spawn_branch_option_supported()


class SpawnPromptStatesTheContract(unittest.TestCase):
    """What the worker is told, because the worker is who creates the branch."""

    def test_the_prompt_names_the_branch_and_forbids_another(self):
        h = Harness()
        self.addCleanup(h.close)
        prompt = build_spawn_prompt(h.cfg, 682, 'Repair the polling resource watcher')
        self.assertIn('ao/issue-682-repair-the-polling-resource-watcher', prompt)
        self.assertIn('#682', prompt)
        self.assertIn('never move it to another', prompt.lower())

    def test_a_branch_given_by_the_controller_is_the_branch_in_the_prompt(self):
        # The prompt must not invent a name of its own: the workspace AO opens
        # and the instruction the worker reads have to be the same branch.
        h = Harness()
        self.addCleanup(h.close)
        prompt = build_spawn_prompt(h.cfg, 682, 'Repair the watcher',
                                    branch='ao/issue-682-from-the-controller')
        self.assertIn('ao/issue-682-from-the-controller', prompt)
        self.assertNotIn('ao/issue-682-repair-the-watcher', prompt)


class TheIncidentDoesNotRepeat(unittest.TestCase):
    """The pairing from the forensic report, run through a real tick."""

    def test_the_prompt_slug_branch_never_creates_a_claim_on_issue_six(self):
        # AO worked issue #948 on ao/implement-github-issue-6/root, and every
        # tool that grepped the branch thought a job on issue #6 had started.
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(948, state='spawned', session_id='mrmap-42')
        ao = fakes.FakeAO([session('mrmap-42', issue=948, branch=INCIDENT[948])])
        gh = fakes.FakeGitHub(issues=[approved_issue(948)],
                              events={948: [label_event('labeled', 'jokiefer')]})
        report = h.tick(gh=gh, ao=ao)
        self.assertEqual(db.get_claim(h.conn, 948)['state'], db.CLAIM_SPAWNED,
                         'the real assignment keeps its claim')
        self.assertIsNone(db.get_claim(h.conn, 6),
                          'the branch slug must not put a claim on issue #6')
        self.assertNotIn(6, h.rows(), 'and must not put a board row there either')
        self.assertNotIn('NEEDS_HUMAN', report['counts'])


if __name__ == '__main__':
    unittest.main()
