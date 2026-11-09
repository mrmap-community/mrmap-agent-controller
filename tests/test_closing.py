"""W2: closing-keyword reconciliation.

Three surfaces, all offline: the spawn prompt must demand the real closing
keyword; the merge path must trust GitHub's closing linkage (the closed-
references payload the fakes serve) and, when it did not fire, ask the human
exactly once -- the controller closes nothing; and ``reconcile-closing``
prints the human action list without mutating anything.
"""
import io
import json
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest import mock

from mrmap_controller import cli, db, github as gh_mod, lifecycle as st
from mrmap_controller.messages import build_spawn_prompt
from tests import fakes
from tests.fakes import Harness, approved_issue, pr
from tests.test_state_compatibility import write_config


class SpawnPromptClosingRules(unittest.TestCase):
    """The prompt that produced the prose-only bodies (#995: 'What this does
    (#981)') must now demand the keyword GitHub actually acts on."""

    def prompt(self, issue=981, title='Absorbed work'):
        h = Harness()
        try:
            return build_spawn_prompt(h.cfg, issue, title)
        finally:
            h.close()

    def test_default_branch_rule_names_the_assigned_issue(self):
        text = self.prompt(981)
        self.assertIn('"Closes #981"', text)
        self.assertIn('default branch', text)
        self.assertIn('own line', text)
        # the assigned number, never a placeholder
        self.assertNotIn('Closes #<N>', text)

    def test_stacked_prs_use_refs_and_never_closes(self):
        text = self.prompt(1177)
        self.assertIn('"Refs #1177"', text)
        self.assertIn('stacks on', text)
        self.assertIn('NEVER "Closes" toward a non-default base', text)

    def test_one_closes_many_refs_and_no_invented_links(self):
        text = self.prompt(682)
        self.assertIn('exactly one issue', text)
        self.assertIn('"Refs #<M>"', text)
        self.assertIn('never invent a link', text)
        # and the prompt stays issue-specific when the number changes
        self.assertIn('Closes #682', text.replace('"', ''))
        self.assertNotIn('981', text)


class MergeClosesTheIssue(unittest.TestCase):
    """merged + the closing linkage fired -> retire quietly."""

    def harness(self, closed_links, issue_state='open'):
        h = Harness(execute=True)
        h.claim(981, 'spawned', session_id='mrmap-77', age_minutes=60)
        db.upsert_issue(h.conn, 981, st.PR_OPEN, fakes.now(),
                        session_id='mrmap-77', pr_number=995)
        gh = fakes.FakeGitHub(
            issues=[approved_issue(981, state=issue_state)],
            prs={995: pr(995, issue=981, state='MERGED')},
            closed_links=closed_links)
        return h, gh

    def test_merged_and_closed_retires_without_asking(self):
        h, gh = self.harness({995: {981}})
        report = h.tick(execute=True, gh=gh)
        self.assertEqual(db.get_claim(h.conn, 981)['state'], db.CLAIM_MERGED)
        self.assertEqual(gh.posted_comments, [])
        self.assertTrue(db.seen(h.conn, 'closing_reconcile', '995:981'))
        reconcile = fakes.actions(report, 'RECONCILE_CLOSING')
        self.assertEqual(reconcile, [])
        self.assertEqual(fakes.actions(report, 'MERGED')[0]['pr'], 995)
        h.close()

    def test_merged_but_issue_closed_by_human_needs_no_ask(self):
        h, gh = self.harness({}, issue_state='closed')
        h.tick(execute=True, gh=gh)
        self.assertEqual(gh.posted_comments, [])
        self.assertTrue(db.seen(h.conn, 'closing_reconcile', '995:981'))
        h.close()


class MergeWithoutClosureAsksOnce(unittest.TestCase):
    """The #981/#995 pattern: merged PR, prose-only body, issue still open.
    Exactly one comment asks the human to close; the issue itself is
    untouched and the controller never closes anything."""

    def driven(self, execute=True, fail_post=False):
        h = Harness(execute=execute)
        h.claim(981, 'spawned', session_id='mrmap-77', age_minutes=60)
        db.upsert_issue(h.conn, 981, st.PR_OPEN, fakes.now(),
                        session_id='mrmap-77', pr_number=995)
        gh = fakes.FakeGitHub(issues=[approved_issue(981)],
                              prs={995: pr(995, issue=981, state='MERGED')},
                              closed_links={})
        if fail_post:
            gh.fail_on = {'post_issue_comment'}
        report = h.tick(execute=execute, gh=gh)
        return h, gh, report

    def test_one_ask_comment_and_the_issue_is_left_alone(self):
        h, gh, report = self.driven()
        self.assertEqual(len(gh.posted_comments), 1)
        number, body = gh.posted_comments[0]
        self.assertEqual(number, 981)
        self.assertIn('995', body)
        self.assertIn('still open', body)
        self.assertIn('controller does not close', body)
        # the DB-side truth is still recorded, and the issue's GitHub state
        # was only ever read
        self.assertEqual(db.get_claim(h.conn, 981)['state'], db.CLAIM_MERGED)
        self.assertEqual(db.get_issue(h.conn, 981)['state'], st.MERGED)
        self.assertEqual([a['action'] for a in fakes.actions(report)
                          if a['action'] == 'RECONCILE_CLOSING'],
                         ['RECONCILE_CLOSING'])
        self.assertTrue(db.seen(h.conn, 'closing_reconcile', '995:981'))
        h.close()

    def test_second_tick_never_repeats_the_ask(self):
        h, gh, _ = self.driven()
        # a second tick with the same world (and even an unmarked event): the
        # retired claim leaves the follow loop, so nothing re-posts
        db.mark_events(h.conn, [('closing_reconcile', '995:981', 981, 995, 'x')],
                       fakes.now())
        gh2 = fakes.FakeGitHub(issues=[approved_issue(981)],
                               prs={995: pr(995, issue=981, state='MERGED')},
                               closed_links={})
        h.tick(execute=True, gh=gh2)     # shares h's db
        self.assertEqual(gh2.posted_comments, [])
        h.close()

    def test_dry_run_previews_without_posting_or_consuming(self):
        h, gh, report = self.driven(execute=False)
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(len(fakes.actions(report, 'WOULD_RECONCILE_CLOSING')), 1)
        self.assertFalse(db.seen(h.conn, 'closing_reconcile', '995:981'))
        h.close()

    def test_failed_ask_is_reported_and_left_unmarked(self):
        h, gh, report = self.driven(fail_post=True)
        self.assertEqual(len(fakes.actions(report, 'RECONCILE_CLOSING_FAILED')), 1)
        self.assertFalse(db.seen(h.conn, 'closing_reconcile', '995:981'))
        # the merged retirement itself still happened: the claim is off the
        # hot loop and the human action list (reconcile-closing) is the retry
        self.assertEqual(db.get_claim(h.conn, 981)['state'], db.CLAIM_MERGED)
        h.close()

    def test_unreadable_linkage_asks_nothing_and_warns(self):
        h = Harness(execute=True)
        h.claim(981, 'spawned', session_id='mrmap-77', age_minutes=60)
        db.upsert_issue(h.conn, 981, st.PR_OPEN, fakes.now(),
                        session_id='mrmap-77', pr_number=995)
        gh = fakes.FakeGitHub(issues=[approved_issue(981)],
                              prs={995: pr(995, issue=981, state='MERGED')})
        gh.fail_on = {'pr_closes_issue'}
        h.tick(execute=True, gh=gh)
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(len(h.log.events('closing_linkage_unreadable')), 1)
        h.close()


class ClosingLinkageAdapter(unittest.TestCase):
    """pr_closes_issue reads GitHub's closed-references GraphQL connection
    through the deny-listed runner -- never a prose grep."""

    def runner(self, payload):
        key = ' '.join(['gh', 'api', 'graphql', '-f', 'owner=o', '-f', 'name=n',
                        '-F', 'number=995',
                        '-f', f'query={gh_mod.CLOSED_ISSUES_QUERY}'])
        return fakes.FakeRunner(responses={key: json.dumps(payload)})

    def gh(self, payload):
        return gh_mod.GitHub(self.runner(payload), 'o/n')

    def test_closed_reference_matches(self):
        gh = self.gh({'data': {'repository': {'pullRequest': {'closedIssues': {
            'nodes': [{'number': 981, 'state': 'CLOSED'}]}}}}})
        self.assertTrue(gh.pr_closes_issue(995, 981))

    def test_nothing_closed_or_only_mention_is_false(self):
        empty = {'data': {'repository': {'pullRequest': {'closedIssues':
                                                            {'nodes': []}}}}}
        self.assertFalse(self.gh(empty).pr_closes_issue(995, 981))
        other = {'data': {'repository': {'pullRequest': {'closedIssues': {
            'nodes': [{'number': 1074, 'state': 'CLOSED'}]}}}}}
        self.assertFalse(self.gh(other).pr_closes_issue(995, 981))

    def test_unexpected_shape_fails_closed(self):
        with self.assertRaises(gh_mod.GithubError):
            self.gh({'data': {}}).pr_closes_issue(995, 981)


class ReconcileClosingCommand(unittest.TestCase):
    """The cli surface that should surface the #981/#995 pattern today:
    merged claims whose issue is still open, printed, never mutated."""

    def seed(self, h, issue, pr_number, state='open'):
        h.claim(issue, db.CLAIM_MERGED, session_id='mrmap-77', age_minutes=600)
        db.upsert_issue(h.conn, issue, st.MERGED, fakes.now(), pr_number=pr_number)

    def run_cmd(self, h, responses):
        cfg_path = write_config(h.tmp, h.cfg.state_db, h.cfg.lock_file)
        runner = fakes.FakeRunner(responses=responses, deny=('gh issue close',
                                                             'gh api -X'))
        out = io.StringIO()
        with mock.patch.object(cli, 'controller_runner', return_value=runner):
            with redirect_stdout(out):
                code = cli.cmd_reconcile_closing(
                    Namespace(config=cfg_path, verbose=False))
        return code, out.getvalue(), runner

    def test_lists_open_pair_and_leaves_closed_pair_out(self):
        h = Harness()
        self.seed(h, 981, 995)                          # still open today
        self.seed(h, 1074, 1192)                        # closed by the human
        responses = {
            'gh api repos/mrmap-community/mrmap/issues/981':
                json.dumps({'number': 981, 'state': 'open'}),
            'gh api repos/mrmap-community/mrmap/issues/1074':
                json.dumps({'number': 1074, 'state': 'closed'}),
        }
        code, text, runner = self.run_cmd(h, responses)
        self.assertEqual(code, 0)
        self.assertIn('#981', text)
        self.assertIn('995', text)
        self.assertIn('still open', text)
        self.assertNotIn('#1074', text)
        self.assertIn('never closes', text)
        # prints only: every external call was a plain issue read...
        for argv in runner.calls:
            self.assertTrue(argv[:2] == ['gh', 'api']
                            and argv[2].startswith('repos/')
                            and argv[2].endswith('/issues/981')
                            or argv[2].endswith('/issues/1074'), argv)
        # ...and the state database kept the merged claims untouched
        self.assertEqual(db.get_claim(h.conn, 981)['state'], db.CLAIM_MERGED)
        h.close()

    def test_nothing_merged_means_nothing_to_print(self):
        h = Harness()
        code, text, runner = self.run_cmd(h, {})
        self.assertEqual(code, 0)
        self.assertIn('none', text)
        self.assertEqual(runner.calls, [])
        h.close()
