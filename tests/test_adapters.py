"""Adapters, CI interpretation, config and logging -- driven by a fake runner."""
import json
import unittest

from mrmap_controller import ao as ao_mod, config as config_mod, github as gh_mod
from mrmap_controller.github import interpret_ci
from mrmap_controller.logging_util import redact
from mrmap_controller.messages import build_dispatch_message
from mrmap_controller.runner import Denied, Runner
from tests import fakes
from tests.fakes import FakeRunner


class GitHubAdapter(unittest.TestCase):
    def gh(self, responses):
        return gh_mod.GitHub(FakeRunner(responses), 'mrmap-community/mrmap')

    def test_open_pulls_flattens_every_page(self):
        # --slurp wraps every page in a list, so the payload is a list of lists.
        gh = self.gh({'gh api --paginate --slurp repos/mrmap-community/mrmap/pulls'
                      '?state=open&per_page=100': '[ [{"number":1}], [{"number":2}] ]'})
        self.assertEqual([p['number'] for p in gh.open_pulls()], [1, 2])
        self.assertIn('--paginate', gh.runner.calls[0])
        self.assertIn('--slurp', gh.runner.calls[0])
        self.assertNotIn('-X', gh.runner.calls[0])

    def test_unexpected_shape_fails_closed(self):
        gh = self.gh({'gh api --paginate --slurp repos/mrmap-community/mrmap/pulls'
                      '?state=open&per_page=100': '{"oops": true}'})
        with self.assertRaises(gh_mod.GithubError):
            gh.open_pulls()

    def test_unparsable_json_fails_closed(self):
        gh = self.gh({'gh api --paginate --slurp repos/mrmap-community/mrmap/pulls'
                      '?state=open&per_page=100': 'not json'})
        with self.assertRaises(gh_mod.GithubError):
            gh.open_pulls()

    def test_command_failure_becomes_github_error(self):
        runner = FakeRunner({})
        runner.raise_on['gh api'] = RuntimeError('boom')
        with self.assertRaises(gh_mod.GithubError):
            gh_mod.GitHub(runner, 'a/b').open_pulls()

    def test_only_read_endpoints_are_used(self):
        gh = gh_mod.GitHub(fakes.FakeRunner({}), 'a/b')
        for attempt in (gh.open_pulls, lambda: gh.issue(1),
                        lambda: gh.issue_label_events(1, 'x'),
                        lambda: gh.pr_view(1), lambda: gh.pr_reviews(1),
                        lambda: gh.pr_review_comments(1),
                        lambda: gh.pr_issue_comments(1),
                        lambda: gh.pr_resolved_thread_comment_ids(1),
                        gh.whoami, lambda: gh.open_issues_with_label('agent: approved')):
            with self.assertRaises(gh_mod.GithubError):
                attempt()                      # nothing is answerable -> fail closed
        for argv in gh.runner.calls:
            joined = ' '.join(argv)
            self.assertNotIn(' -X', joined)
            self.assertNotIn('--method', joined)
            self.assertNotIn('--field', joined)
            self.assertFalse(joined.startswith('gh pr merge'))
            self.assertFalse(joined.startswith('gh api graphql -f query=mutation'))

    def test_label_events_are_filtered_and_sorted(self):
        events = [{'event': 'labeled', 'id': 3, 'created_at': '2026-01-03T00:00:00Z',
                   'label': {'name': 'agent: approved'}, 'actor': {'login': 'jokiefer'}},
                  {'event': 'labeled', 'id': 1, 'created_at': '2026-01-01T00:00:00Z',
                   'label': {'name': 'bug'}, 'actor': {'login': 'other'}},
                  {'event': 'unlabeled', 'id': 2, 'created_at': '2026-01-02T00:00:00Z',
                   'label': {'name': 'agent: approved'}, 'actor': {'login': 'other'}}]
        gh = self.gh({'gh api --paginate --slurp repos/mrmap-community/mrmap/issues/7/events'
                      '?per_page=100': json.dumps([events])})
        kept = gh.issue_label_events(7, 'agent: approved')
        self.assertEqual([e['id'] for e in kept], [2, 3])

    def test_pr_view_checks_the_number(self):
        gh = self.gh({'gh pr view 9 --repo mrmap-community/mrmap --json '
                      + gh_mod.PR_FIELDS: json.dumps({'number': 8})})
        with self.assertRaises(gh_mod.GithubError):
            gh.pr_view(9)

    def test_thread_lookup_returns_string_ids(self):
        payload = {'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': [
            {'isResolved': True, 'isOutdated': False,
             'comments': {'nodes': [{'databaseId': 11}]}},
            {'isResolved': False, 'isOutdated': True,
             'comments': {'nodes': [{'databaseId': 12}]}}]}}}}}

        class GraphQLRunner(fakes.FakeRunner):
            """Answers only the graphql call and records how it was built."""

            def __init__(self):
                super().__init__({})
                self.argv = None

            def run(self, argv, **kwargs):
                self.argv = [str(a) for a in argv]
                self.calls.append(self.argv)
                return type('P', (), {'stdout': json.dumps(payload),
                                       'stderr': '', 'returncode': 0})()

        runner = GraphQLRunner()
        gh = gh_mod.GitHub(runner, 'mrmap-community/mrmap')
        self.assertEqual(gh.pr_resolved_thread_comment_ids(5), {'11', '12'})
        self.assertEqual(runner.argv[:3], ['gh', 'api', 'graphql'])
        self.assertIn('number=5', ' '.join(runner.argv))
        self.assertNotIn('-X', runner.argv)

    def test_thread_lookup_malformed_fails_closed(self):
        runner = FakeRunner({'x': ''})

        class Bad(FakeRunner):
            def run(self, argv, **kwargs):
                self.calls.append(list(argv))
                return type('P', (), {'stdout': '{"data": {}}', 'stderr': '',
                                       'returncode': 0})()

        with self.assertRaises(gh_mod.GithubError):
            gh_mod.GitHub(Bad(), 'a/b').pr_resolved_thread_comment_ids(5)

    def test_reference_matching_needs_a_whole_number(self):
        ref = gh_mod.GitHub.pr_references_issue
        self.assertTrue(ref({'title': 'fix thing (#1203)', 'body': '',
                             'head': {'ref': 'x'}}, 1203))
        self.assertFalse(ref({'title': 'fix thing (#1203)', 'body': '',
                              'head': {'ref': 'x'}}, 120))
        self.assertTrue(ref({'title': '', 'body': 'Closes #120.', 'head': {'ref': ''}},
                            120))
        self.assertTrue(ref({'title': '', 'body': '',
                             'head': {'ref': 'ao/issue-120/thing'}}, 120))
        self.assertFalse(ref({'title': '', 'body': '',
                              'head': {'ref': 'ao/issue-1203/thing'}}, 120))


class CiInterpretation(unittest.TestCase):
    def test_all_success(self):
        status, _ = interpret_ci([{'name': 'a', 'status': 'COMPLETED',
                                   'conclusion': 'SUCCESS'},
                                  {'name': 'b', 'status': 'COMPLETED',
                                   'conclusion': 'SKIPPED'}])
        self.assertEqual(status, 'success')

    def test_one_failure_wins(self):
        status, checks = interpret_ci([{'name': 'a', 'status': 'COMPLETED',
                                        'conclusion': 'SUCCESS'},
                                       {'name': 'b', 'status': 'COMPLETED',
                                        'conclusion': 'FAILURE',
                                        'detailsUrl': 'u'}])
        self.assertEqual(status, 'failure')
        self.assertEqual(checks[0]['name'], 'b')

    def test_in_progress_is_pending(self):
        status, _ = interpret_ci([{'name': 'a', 'status': 'IN_PROGRESS',
                                   'conclusion': None}])
        self.assertEqual(status, 'pending')

    def test_empty_rollup_is_unknown(self):
        self.assertEqual(interpret_ci([])[0], 'unknown')
        self.assertEqual(interpret_ci(None)[0], 'unknown')

    def test_checks_from_another_commit_are_unknown(self):
        status, _ = interpret_ci([{'name': 'a', 'status': 'COMPLETED',
                                   'conclusion': 'FAILURE', 'headSha': 'b' * 40}],
                                 expected_sha='a' * 40)
        self.assertEqual(status, 'unknown')

    def test_action_required_counts_as_failure(self):
        status, _ = interpret_ci([{'name': 'a', 'status': 'COMPLETED',
                                   'conclusion': 'ACTION_REQUIRED'}])
        self.assertEqual(status, 'failure')


class AOAdapter(unittest.TestCase):
    LIST = {'id': 'mrmap-9', 'projectId': 'mrmap', 'role': 'worker',
            'status': 'mergeable', 'activity': 'idle', 'isTerminated': False,
            'prs': []}

    def runner(self):
        detail = {'session': {'id': 'mrmap-9', 'projectId': 'mrmap', 'kind': 'worker',
                              'issueId': '120', 'activity': {'state': 'idle'},
                              'isTerminated': False,
                              'prs': [{'number': 500, 'state': 'open'}]}}
        return FakeRunner({'ao session ls --project mrmap --json --include-terminated':
                           json.dumps({'data': [self.LIST]}),
                           'ao session get mrmap-9 --project mrmap --json':
                           json.dumps(detail)})

    def test_sessions_are_enriched_with_the_issue_association(self):
        ao = ao_mod.AO(self.runner(), 'mrmap')
        sessions = ao.sessions()
        self.assertEqual(sessions[0].issue, 120)
        self.assertEqual(sessions[0].activity, 'idle')
        self.assertEqual(sessions[0].pr_numbers, [500])
        self.assertEqual(ao.session_for_issue(120)[0].id, 'mrmap-9')
        self.assertEqual(ao.session_for_issue(121), [])

    def test_listing_without_data_fails_closed(self):
        ao = ao_mod.AO(FakeRunner({'ao session ls --project mrmap --json '
                                   '--include-terminated': '{"data": null}'}), 'mrmap')
        with self.assertRaises(ao_mod.AOError):
            ao.sessions()

    def test_resume_of_a_session_that_stays_exited_is_an_error(self):
        runner = self.runner()
        stuck = {'session': {'id': 'mrmap-9', 'issueId': '120', 'kind': 'worker',
                             'activity': {'state': 'exited'}, 'isTerminated': False}}
        runner.responses['ao session resume-agent mrmap-9 --project mrmap --json'] = \
            json.dumps({'ok': True})
        runner.responses['ao session get mrmap-9 --project mrmap --json'] = \
            json.dumps(stuck)
        ao = ao_mod.AO(runner, 'mrmap')
        with self.assertRaises(ao_mod.AOError):
            ao.resume_agent('mrmap-9')

    def test_terminated_session_is_neither_executing_nor_resumable(self):
        sess = ao_mod.Session({'id': 'mrmap-2', 'activity': {'state': 'exited'},
                               'isTerminated': True, 'workspaceCleanup': 'removed'})
        self.assertFalse(sess.executing)
        self.assertFalse(sess.resumable)
        self.assertFalse(sess.connected)

    def test_idle_and_active_states(self):
        self.assertTrue(ao_mod.Session({'id': 'x', 'activity': {'state': 'active'},
                                        'isTerminated': False}).executing)
        self.assertFalse(ao_mod.Session({'id': 'x', 'activity': {'state': 'idle'},
                                         'isTerminated': False}).executing)
        self.assertTrue(ao_mod.Session({'id': 'x', 'activity': {'state': 'idle'},
                                        'isTerminated': False}).connected)
        self.assertTrue(ao_mod.Session({'id': 'x', 'activity': {'state': 'exited'},
                                        'isTerminated': False}).resumable)

    def test_session_id_is_parsed_from_spawn_output(self):
        ao = ao_mod.AO(FakeRunner({}), 'mrmap')
        self.assertEqual(ao.session_id_from_output(
            'spawned session mrmap-12 "Issue #5" (idle)'), 'mrmap-12')
        self.assertIsNone(ao.session_id_from_output('no identifier here'))


class ConfigBehaviour(unittest.TestCase):
    def test_unknown_key_is_refused(self):
        import pathlib
        import tempfile
        path = pathlib.Path(tempfile.mkdtemp()) / 'config.json'
        path.write_text('{"max_workers": 1, "max_wokers": 2}')
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(path)

    def test_environment_override(self):
        import pathlib
        import tempfile
        path = pathlib.Path(tempfile.mkdtemp()) / 'config.json'
        path.write_text('{"max_workers": 1}')
        cfg = config_mod.load(path, env={'MRMAP_CONTROLLER_MAX_WORKERS': '3'})
        self.assertEqual(cfg.max_workers, 3)

    def test_boolean_override_and_list_override(self):
        cfg = config_mod.load('/nonexistent/config.json', env={
            'MRMAP_CONTROLLER_DRY_RUN': 'false',
            'MRMAP_CONTROLLER_APPROVERS': 'alice, bob'})
        self.assertFalse(cfg.dry_run)
        self.assertEqual(cfg.approvers, ['alice', 'bob'])

    def test_missing_explicit_file_is_an_error(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(None, env={'MRMAP_CONTROLLER_CONFIG':
                                       '/nonexistent/mrmap-config.json'})

    def test_empty_approver_list_is_refused(self):
        values = dict(config_mod.DEFAULTS)
        values['approvers'] = []
        with self.assertRaises(config_mod.ConfigError):
            config_mod._validate(config_mod.Config(values))

    def test_shipped_config_matches_the_documented_defaults(self):
        cfg = config_mod.load(str(config_mod.BASE / 'config.json'), env={})
        self.assertTrue(cfg.dry_run, 'the shipped default must be a dry run')
        self.assertLessEqual(cfg.max_workers, 3)
        self.assertEqual(cfg.approvers, ['jokiefer'])
        self.assertEqual(cfg.poll_interval_minutes, 5)


class Redaction(unittest.TestCase):
    def test_tokens_and_urls_are_masked(self):
        self.assertIn('[redacted]', redact('token ghp_' + 'a' * 36))
        self.assertIn('[redacted]', redact('https://x-access-token:SECRET@github.com'))
        self.assertIn('[redacted]', redact('Authorization: Bearer abc123'))
        self.assertIn('[redacted]', redact('password=hunter2'))

    def test_long_text_is_truncated(self):
        self.assertLessEqual(len(redact('x' * 5000)), 420)


class MessageContent(unittest.TestCase):
    def test_wrapper_path_and_prohibitions_are_present(self):
        values = dict(config_mod.DEFAULTS)
        cfg = config_mod.Config(values)
        text = build_dispatch_message(cfg, issue=120, pr_number=500,
                                      pr_url='https://x/500', head_sha='a' * 40,
                                      ci_failures=[{'name': 'lint',
                                                    'conclusion': 'FAILURE',
                                                    'url': 'u'}],
                                      review_items=[], mode='execute')
        self.assertIn(cfg.docker_test_wrapper, text)
        self.assertIn('Do not approve, merge', text)
        self.assertIn('Never run global Docker cleanup', text)
        self.assertNotIn('approve this pull request', text.lower())

    def test_message_length_is_bounded(self):
        values = dict(config_mod.DEFAULTS)
        values['message_max_chars'] = 400
        cfg = config_mod.Config(values)
        text = build_dispatch_message(cfg, issue=1, pr_number=2, pr_url='u',
                                      head_sha='s', ci_failures=[
                                          {'name': 'n' * 200, 'conclusion': 'FAILURE',
                                           'url': 'u'}] * 40,
                                      review_items=[], mode='execute')
        self.assertLessEqual(len(text), 460)


class RunnerDenyList(unittest.TestCase):
    def test_destructive_commands_are_refused_before_any_exec(self):
        # subprocess.run is replaced by a trap: a refused command must never
        # reach the operating system, not even as a failed attempt.
        import mrmap_controller.runner as runner_module
        def trap(argv, *args, **kwargs):
            raise AssertionError(f'reached subprocess: {argv}')
        original = runner_module.subprocess.run
        runner_module.subprocess.run = trap
        self.addCleanup(setattr, runner_module.subprocess, 'run', original)
        runner = Runner()
        for argv in (['gh', 'pr', 'merge', '1103'],
                     ['gh', 'API', '-x', 'POST', 'repos/x/y/issues/1/labels'],
                     ['gh', 'pr', 'review', '--approve'],
                     ['gh', 'pr', 'close', '1103'],
                     ['gh', 'api', '-X', 'POST', 'repos/x/y/issues/1/labels'],
                     ['gh', 'api', '--method', 'DELETE', 'x'],
                     ['gh', 'issue', 'edit', '1', '--remove-label', 'agent: approved'],
                     ['ao', 'pr', 'merge', '1103'],
                     ['ao', 'pr', 'resolve-comments', '1103'],
                     ['ao', 'session', 'kill', 'mrmap-7'],
                     ['ao', 'session', 'cleanup', '-y'],
                     ['git', 'push', '--force', 'origin', 'HEAD'],
                     ['git', 'push', '-f'],
                     ['docker', 'compose', 'down', '--volumes'],
                     ['docker', 'system', 'prune'],
                     ['docker', 'ps']):
            with self.assertRaises(Denied, msg=f'{argv} must be refused'):
                runner.run(argv)

    def test_the_reads_and_writes_the_controller_needs_are_allowed(self):
        from mrmap_controller.runner import assert_allowed
        for argv in (['gh', 'api', 'user'],
                     ['gh', 'api', '--paginate', '--slurp', 'repos/a/b/pulls'],
                     ['gh', 'pr', 'view', '1', '--json', 'state'],
                     ['gh', 'api', 'graphql', '-f', 'query=...'],
                     ['ao', 'spawn', '--project', 'mrmap', '--kind', 'worker'],
                     ['ao', 'send', '--session', 'mrmap-9', '--message', 'x'],
                     ['ao', 'session', 'resume-agent', 'mrmap-9'],
                     ['python3', '/host/scripts/agent_capacity']):
            assert_allowed(argv)


if __name__ == '__main__':
    unittest.main()
