"""Loop integrity behaviours: W1 state-machine convergence, W4 isolation /
backoff / watchdog / single instance, W5 board refresh.

All offline. GitHub and AO are the doubles from tests/fakes.py; the board
refresher is recorded by the harness, so no test runs ``scripts/wip``, touches
the network or posts anything anywhere.
"""
import datetime as dt
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from mrmap_controller import cli, config as config_mod, core, db, lifecycle as st
from tests import fakes
from tests.fakes import (Harness, approved_issue, label_event, open_pr_entry,
                        pr, session)


def minutes_from_now(minutes):
    return (dt.datetime.now(dt.timezone.utc)
            + dt.timedelta(minutes=minutes)).isoformat(timespec='seconds')


class ClaimsIssuesConvergence(unittest.TestCase):
    """W1: claims is the source of truth, issues is its derived projection."""

    def test_reconcile_and_follow_in_one_tick_never_write_running(self):
        # The live contradiction of the audit: claim quarantined, but the
        # issues row rewritten to running by the dead session association.
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(981, state='needs_reconciliation', session_id='mrmap-77')
        gh = fakes.FakeGitHub(issues=[approved_issue(981)],
                              events={981: [label_event('labeled', 'jokiefer')]})
        ao = fakes.FakeAO([session('mrmap-77', issue=981, terminated=True)])
        report = h.tick(gh=gh, ao=ao)
        self.assertNotIn('RUNNING', str(report['counts']))
        row = h.rows()[981]
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(db.get_claim(h.conn, 981)['state'],
                         db.CLAIM_RECONCILE, 'the claim is the truth; it stays put')

    def test_quarantine_and_follow_projection_in_the_same_tick(self):
        # Freshly quarantined this very tick (reconcile stage) -- the follow
        # stage that runs after it in the same tick must not undo it.
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(981, state='spawned', session_id='mrmap-77')
        gh = fakes.FakeGitHub(issues=[approved_issue(981)],
                              events={981: [label_event('labeled', 'jokiefer')]})
        ao = fakes.FakeAO([session('mrmap-77', issue=981, terminated=True)])
        report = h.tick(gh=gh, ao=ao)
        self.assertIn('NEEDS_HUMAN', report['counts'])
        self.assertEqual(h.rows()[981]['state'], 'failed')
        self.assertNotEqual(h.rows()[981]['state'], 'running')

    def test_live_worker_projection_is_untouched(self):
        # Regression guard: the convergence rule must not swallow the honest
        # running projection of a live, held claim.
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state='spawned', session_id='mrmap-7')
        gh = fakes.FakeGitHub(issues=[approved_issue(120)],
                              events={120: [label_event('labeled', 'jokiefer')]})
        ao = fakes.FakeAO([session('mrmap-7', issue=120)])
        h.tick(gh=gh, ao=ao)
        self.assertEqual(h.rows()[120]['state'], 'running')

    def test_merged_claim_is_retired_from_the_hot_loop(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state='merged', session_id='mrmap-7')
        gh = fakes.FakeGitHub(
            issues=[approved_issue(120)],
            events={120: [label_event('labeled', 'jokiefer')]},
            pulls=[open_pr_entry(500, 120)],
            prs={500: pr(500, 120, state='MERGED')},
            reviews={500: []})
        ao = fakes.FakeAO([session('mrmap-7', issue=120, terminated=True)])
        report = h.tick(gh=gh, ao=ao)
        self.assertEqual(gh.with_name('pr_view'), [],
                         'a merged claim must never re-fetch its pull request')
        self.assertNotIn('MERGED', report['counts'])
        self.assertIsNotNone(db.get_claim(h.conn, 120),
                             'retired means skipped, not deleted: the audit row stays')

    # -- the residual the W1 tail left open: a quarantined claim whose issue
    #    still has an open pull request -------------------------------
    def _quarantine_with_pr(self, **pr_kwargs):
        """The live #1009 shape: claim quarantined, PR #1114 open and CI green."""
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(1009, state='needs_reconciliation', session_id='mrmap-13')
        db.upsert_issue(h.conn, 1009, 'failed', fakes.now(),
                        session_id='mrmap-13', pr_number=1114)
        gh = fakes.FakeGitHub(
            issues=[approved_issue(1009)],
            events={1009: [label_event('labeled', 'jokiefer')]},
            pulls=[open_pr_entry(1114, 1009)],
            prs={1114: pr(1114, 1009, **pr_kwargs)},
            reviews={1114: []})
        ao = fakes.FakeAO([session('mrmap-13', issue=1009, terminated=True,
                                   prs=[{'number': 1114}])])
        return h, gh, ao

    def test_quarantined_claim_with_open_pr_never_proceeds(self):
        # The residual of D-risk 2: the W1 guard covered the pr_number is None
        # tail only, so the pull-request path kept writing live-looking states.
        h, gh, ao = self._quarantine_with_pr()
        report = h.tick(gh=gh, ao=ao)
        row = h.rows()[1009]
        self.assertEqual(row['state'], 'failed',
                         f'projection said {row["state"]!r} for a quarantined claim')
        self.assertEqual(row['pr_number'], 1114,
                         'the pull request stays recorded: reconciliation needs it')
        self.assertIn('IDLE', report['counts'],
                      'the pull request is still read and its events still retired')
        self.assertEqual(db.get_claim(h.conn, 1009)['state'], db.CLAIM_RECONCILE)

    def test_quarantined_claim_with_running_checks_never_proceeds(self):
        h, gh, ao = self._quarantine_with_pr(checks=fakes.pending_checks())
        h.tick(gh=gh, ao=ao)
        self.assertEqual(h.rows()[1009]['state'], 'failed')

    def test_quarantined_claim_still_reports_a_ready_pull_request(self):
        # Clamping is about hiding progress, never about hiding a human action:
        # "CI green and approved, a human merges" must stay visible.
        h, gh, ao = self._quarantine_with_pr(review_decision='APPROVED')
        report = h.tick(gh=gh, ao=ao)
        self.assertIn('READY', report['counts'])
        self.assertEqual(h.rows()[1009]['state'], 'ready_for_human_merge')

    def test_quarantined_claim_is_retired_when_the_pr_merges(self):
        h, gh, ao = self._quarantine_with_pr(state='MERGED')
        report = h.tick(gh=gh, ao=ao)
        self.assertIn('MERGED', report['counts'])
        self.assertEqual(h.rows()[1009]['state'], 'merged')
        self.assertEqual(db.get_claim(h.conn, 1009)['state'], db.CLAIM_MERGED,
                         'a merged pull request settles the quarantine for real')


class PerPrIsolation(unittest.TestCase):
    """W4: one poison pull request may not abort the tick."""

    def setUp(self):
        self.h = Harness(execute=True)
        self.addCleanup(self.h.close)
        self.h.claim(201, state='spawned', session_id='mrmap-7')
        self.h.claim(202, state='spawned', session_id='mrmap-8')
        self.ao = fakes.FakeAO([
            session('mrmap-7', issue=201, prs=[{'number': 601}]),
            session('mrmap-8', issue=202, prs=[{'number': 602}])])
        self.gh = fakes.FakeGitHub(
            issues=[approved_issue(201), approved_issue(202)],
            pulls=[open_pr_entry(601, 201), open_pr_entry(602, 202)],
            prs={601: pr(601, 201), 602: pr(602, 202)},
            reviews={601: [], 602: []})
        # the incident's fingerprint, raised for exactly one pull request
        self.gh.pr_view_raises[601] = sqlite3.OperationalError(
            'no such column: attempts')

    def test_poison_pr_is_isolated_and_the_backoff_opens(self):
        report = self.h.tick(gh=self.gh, ao=self.ao)     # must not raise
        self.assertIn('PR_FOLLOW_FAILED', report['counts'])
        self.assertIn('IDLE', report['counts'],
                      'the healthy pull request was still processed')
        err = db.dispatch_for(self.h.conn, 'follow_error:201')
        self.assertEqual((err['status'], err['pr_number']), ('failed', 601))
        self.assertEqual(err['kind'], 'follow_error')
        self.assertEqual(db.attempt_count(self.h.conn, 601, ('ci', 'combined')), 0,
                         'the error row must not spend the worker delivery budgets')

    def test_backoff_defers_the_poison_and_lets_the_others_run(self):
        self.h.tick(gh=self.gh, ao=self.ao)
        report = self.h.tick(gh=self.gh, ao=self.ao)
        self.assertIn('DEFER', report['counts'])
        self.assertNotIn('PR_FOLLOW_FAILED', report['counts'],
                         'inside the window the poison is not re-attempted')
        poisoned = [c for c in self.gh.calls if c[0] == 'pr_view' and c[1]['number'] == 601]
        self.assertEqual(len(poisoned), 1, 'poison view happened exactly once')
        healthy = [c for c in self.gh.calls if c[0] == 'pr_view' and c[1]['number'] == 602]
        self.assertEqual(len(healthy), 2, 'the healthy PR is followed every tick')

    def test_after_the_window_the_poison_is_retried_once_with_a_longer_wait(self):
        self.h.tick(gh=self.gh, ao=self.ao)
        # simulate the window passing without waiting five minutes of real time
        self.h.conn.execute(
            "UPDATE dispatches SET next_retry_at=? WHERE dedupe_key='follow_error:201'",
            (minutes_from_now(-1),))
        self.h.conn.commit()
        report = self.h.tick(gh=self.gh, ao=self.ao)
        self.assertIn('PR_FOLLOW_FAILED', report['counts'])
        err = db.dispatch_for(self.h.conn, 'follow_error:201')
        self.assertEqual(err['attempts'], 2)
        target = dt.datetime.fromisoformat(err['next_retry_at'])
        delay = (target - dt.datetime.now(dt.timezone.utc)).total_seconds()
        self.assertAlmostEqual(delay, 600, delta=90,
                               msg='second occurrence: window doubled to 10 min')


class ControllerBackoff(unittest.TestCase):
    """W4: doubling from the tick interval, capped at six hours."""

    def cfg(self, poll=5):
        values = dict(config_mod.DEFAULTS)
        values['poll_interval_minutes'] = poll
        return config_mod.Config(values)

    def test_sequence_5_10_20_to_the_6h_cap(self):
        cfg = self.cfg(5)
        got = [core._backoff_delay(cfg, n) / 60.0 for n in range(1, 10)]
        self.assertEqual(got, [5, 10, 20, 40, 80, 160, 320, 360, 360])

    def test_sequence_from_other_intervals(self):
        self.assertEqual(core._backoff_delay(self.cfg(1), 1), 60)
        self.assertEqual(core._backoff_delay(self.cfg(1), 2), 120)
        self.assertEqual(core._backoff_delay(self.cfg(10), 7), 6 * 3600)

    def test_retry_wait_semantics(self):
        self.assertIsNone(core._retry_wait(None, fakes.now()))
        self.assertIsNone(core._retry_wait('garbage', fakes.now()))
        self.assertIsNone(core._retry_wait(minutes_from_now(-5), fakes.now()))
        self.assertIsNotNone(core._retry_wait(minutes_from_now(5), fakes.now()))


class StallWatchdog(unittest.TestCase):
    """W4: three consecutive aborted ticks alert the operator once."""

    def setUp(self):
        self.h = Harness(execute=True)
        self.addCleanup(self.h.close)
        self.gh = fakes.FakeGitHub()

    def write_runs(self, outcomes):
        for outcome in outcomes:
            rid = db.start_run(self.h.conn, 'execute', fakes.now())
            db.finish_run(self.h.conn, rid, fakes.now(), outcome)

    def test_watchdog_needs_three_consecutive_aborts(self):
        self.write_runs(['ok', 'aborted', 'aborted'])
        self.assertFalse(core.loop_watchdog(self.h.conn, self.gh, self.h.log, 917))
        self.assertEqual(self.gh.posted_comments, [])
        self.write_runs(['aborted'])
        self.assertTrue(core.loop_watchdog(self.h.conn, self.gh, self.h.log, 917))

    def test_fires_once_per_episode_with_a_critical_log_line(self):
        self.write_runs(['aborted', 'aborted', 'aborted'])
        self.assertTrue(core.loop_watchdog(self.h.conn, self.gh, self.h.log, 917))
        self.assertEqual(len(self.gh.posted_comments), 1)
        number, body = self.gh.posted_comments[0]
        self.assertEqual(number, 917, 'the alert belongs on the work board issue')
        self.assertIn('3 consecutive ticks', body)
        critical = [r for r in self.h.log.records if r[0] == 50]
        self.assertEqual([e for _, e, _ in critical], ['loop_stalled'],
                         'one CRITICAL line for the streak (lands in loop.log)')
        # still the same episode: no second comment, however long it drags on
        self.write_runs(['aborted', 'aborted'])
        self.assertFalse(core.loop_watchdog(self.h.conn, self.gh, self.h.log, 917))
        self.assertFalse(core.loop_watchdog(self.h.conn, self.gh, self.h.log, 917))
        self.assertEqual(len(self.gh.posted_comments), 1)
        self.assertTrue(db.seen(self.h.conn, 'loop_stall', 'stall:0'),
                        'the dedupe rides on processed_events, not memory')

    def test_a_recovered_loop_can_alert_again_for_the_next_episode(self):
        self.write_runs(['aborted', 'aborted', 'aborted'])
        self.assertTrue(core.loop_watchdog(self.h.conn, self.gh, self.h.log, 917))
        self.write_runs(['ok'])
        self.write_runs(['aborted', 'aborted', 'aborted'])
        self.assertTrue(core.loop_watchdog(self.h.conn, self.gh, self.h.log, 917))
        self.assertEqual(len(self.gh.posted_comments), 2,
                         'anchor moved: a new episode may notify once')

    def test_a_failed_post_stays_unmarked_and_retries_next_tick(self):
        self.write_runs(['aborted', 'aborted', 'aborted'])
        self.gh.fail_on.add('post_issue_comment')
        self.assertFalse(core.loop_watchdog(self.h.conn, self.gh, self.h.log, 917))
        self.assertTrue(self.h.log.events('loop_stall_alert_failed'))
        self.gh.fail_on.discard('post_issue_comment')
        self.assertTrue(core.loop_watchdog(self.h.conn, self.gh, self.h.log, 917),
                        'the alert was never delivered, so it must be retried')
        self.assertEqual(len(self.gh.posted_comments), 1)

    def test_watchdog_never_touches_the_scheduler(self):
        # The package promise: alert only, never restart. Behaviorally, a
        # healthy ledger keeps the watchdog completely inert -- no streak,
        # no log, no comment, and the loop code path holds no kill/restart.
        self.write_runs(['ok'] * 3)
        self.assertFalse(core.loop_watchdog(self.h.conn, self.gh, self.h.log, 917))
        self.assertEqual(self.gh.posted_comments, [])
        self.assertEqual(self.h.log.events('loop_stalled'), [])


class LoopSingleInstance(unittest.TestCase):
    """W4: flock refuses a second loop; ticks are untouched."""

    def cfg(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        return h.cfg

    def test_second_loop_is_refused_while_the_first_holds(self):
        cfg = self.cfg()
        log = fakes._Capturing()
        first = cli.loop_instance_lock(cfg, log)
        self.assertIsNotNone(first)
        self.assertIsNone(cli.loop_instance_lock(cfg, log),
                          'a second loop must be refused, not queued')
        self.assertTrue(log.events('loop_instance_locked'))
        first.close()
        third = cli.loop_instance_lock(cfg, log)
        self.assertIsNotNone(third, 'the lock frees when the holder dies')
        third.close()

    def test_lock_lives_beside_the_state_database(self):
        from pathlib import Path
        cfg = self.cfg()
        handle = cli.loop_instance_lock(cfg, fakes._Capturing())
        self.addCleanup(handle.close)
        self.assertTrue((Path(cfg.state_db).parent / 'loop.lock').exists(),
                        '<state_dir>/loop.lock, as the package specifies')


class BoardRefresh(unittest.TestCase):
    """W5: scripts/wip regenerates the board, but only after a change."""

    def test_idle_tick_calls_board_zero_times(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state='spawned', session_id='mrmap-7')
        gh = fakes.FakeGitHub(
            issues=[approved_issue(120)],
            pulls=[open_pr_entry(500, 120)],
            prs={500: pr(500, 120)}, reviews={500: []})
        ao = fakes.FakeAO([session('mrmap-7', issue=120, prs=[{'number': 500}])])
        report = h.tick(gh=gh, ao=ao)
        self.assertIn('IDLE', report['counts'])
        self.assertEqual(h.board_calls, [],
                         'a tick that moved no claim must not touch the board')

    def test_spawn_tick_calls_board_exactly_once(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        gh = fakes.FakeGitHub(issues=[approved_issue(120)],
                              events={120: [label_event('labeled', 'jokiefer')]})
        report = h.tick(gh=gh, ao=fakes.FakeAO([]))
        self.assertIn('SPAWNED', report['counts'])
        self.assertEqual(len(h.board_calls), 1)

    def test_quarantine_tick_calls_board_exactly_once(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(981, state='spawned', session_id='mrmap-77', age_minutes=30)
        gh = fakes.FakeGitHub(issues=[approved_issue(981)],
                              events={981: [label_event('labeled', 'jokiefer')]})
        ao = fakes.FakeAO([session('mrmap-77', issue=981, terminated=True)])
        report = h.tick(gh=gh, ao=ao)
        self.assertIn('NEEDS_HUMAN', report['counts'])
        self.assertEqual(len(h.board_calls), 1)

    def test_merge_retire_tick_calls_board_exactly_once(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(120, state='spawned', session_id='mrmap-7')
        gh = fakes.FakeGitHub(
            issues=[approved_issue(120)],
            pulls=[open_pr_entry(500, 120)],
            prs={500: pr(500, 120, state='MERGED')}, reviews={500: []})
        ao = fakes.FakeAO([session('mrmap-7', issue=120, prs=[{'number': 500}])])
        report = h.tick(gh=gh, ao=ao)
        self.assertIn('MERGED', report['counts'])
        self.assertEqual(len(h.board_calls), 1)

    def test_dry_run_moves_no_claim_and_calls_no_board(self):
        h = Harness(execute=False)
        self.addCleanup(h.close)
        gh = fakes.FakeGitHub(issues=[approved_issue(120)],
                              events={120: [label_event('labeled', 'jokiefer')]})
        report = h.tick(execute=False, gh=gh, ao=fakes.FakeAO([]))
        self.assertIn('WOULD_SPAWN', report['counts'])
        self.assertEqual(h.board_calls, [])


class RefreshBoardHelper(unittest.TestCase):
    """W5 unit: lifecycle.refresh_board runs wip and survives every failure."""

    def setUp(self):
        # scripts/wip resolves its repository from the current directory, so a
        # realistic fixture is a real directory layout: <repo>/scripts/wip.
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='mrmap-wip-board-test-'))
        self.repo = self.tmp / 'mrmap'
        (self.repo / 'scripts').mkdir(parents=True)
        self.script = self.repo / 'scripts' / 'wip'
        self.script.write_text('#!/bin/sh\nexit 0\n')
        self.script.chmod(0o755)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, script=None):
        values = dict(config_mod.DEFAULTS)
        values['wip_script'] = str(self.script) if script is None else script
        return config_mod.Config(values)

    def test_board_repository_is_the_checkout_that_carries_the_script(self):
        self.assertEqual(st.board_repository(str(self.script)), str(self.repo))
        self.assertIsNone(st.board_repository(''), 'unset means no repository')

    def test_invokes_wip_board_with_the_command_timeout(self):
        log = fakes._Capturing()
        done = type('P', (), {'returncode': 0, 'stdout': 'board ok', 'stderr': ''})
        with mock.patch.object(st.subprocess, 'run', return_value=done) as run:
            self.assertTrue(st.refresh_board(self.cfg(), log))
        run.assert_called_once_with([str(self.script), 'board'],
                                    capture_output=True, text=True, timeout=120,
                                    cwd=str(self.repo))
        self.assertTrue(log.events('board_refreshed'))

    def test_board_runs_inside_the_repository_not_the_controller_install(self):
        # scripts/wip resolves the repository with `gh repo view` and
        # `git rev-parse`, both of which read the working directory. Run from
        # the controller's install directory it cannot see the application
        # repository and `wip board` fails, which silently disables W5.
        log = fakes._Capturing()
        done = type('P', (), {'returncode': 0, 'stdout': '', 'stderr': ''})
        with mock.patch.object(st.subprocess, 'run', return_value=done) as run:
            st.refresh_board(self.cfg(), log)
        self.assertEqual(pathlib.Path(run.call_args.kwargs['cwd']),
                         self.repo.resolve(),
                         'the board tool must run in its own checkout')

    def test_wip_failure_is_a_warning_not_a_tick_problem(self):
        log = fakes._Capturing()
        bad = type('P', (), {'returncode': 1, 'stdout': '', 'stderr': 'gh: boom'})
        with mock.patch.object(st.subprocess, 'run', return_value=bad):
            self.assertFalse(st.refresh_board(self.cfg(), log))
        self.assertTrue(log.events('board_refresh_failed'))

    def test_missing_script_is_a_warning_without_a_subprocess(self):
        log = fakes._Capturing()
        with mock.patch.object(st.subprocess, 'run') as run:
            self.assertFalse(st.refresh_board(self.cfg(str(self.tmp / 'gone')), log))
        run.assert_not_called()
        self.assertTrue(log.events('board_refresh_failed'))

    def test_missing_or_broken_script_is_a_warning(self):
        log = fakes._Capturing()
        with mock.patch.object(st.subprocess, 'run',
                               side_effect=FileNotFoundError('no wip')):
            self.assertFalse(st.refresh_board(self.cfg(), log))
        self.assertTrue(log.events('board_refresh_failed'))

    def test_unset_script_skips_without_subprocess(self):
        log = fakes._Capturing()
        with mock.patch.object(st.subprocess, 'run') as run:
            self.assertFalse(st.refresh_board(self.cfg(''), log))
        run.assert_not_called()

    def test_tick_survives_a_board_refresher_that_raises(self):
        # the wiring promise: no board outcome can abort a tick
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.claim(981, state='spawned', session_id='mrmap-77', age_minutes=30)

        def angry():
            raise RuntimeError('board on fire')
        controller = h.controller()
        controller.board_refresher = angry
        gh = fakes.FakeGitHub(issues=[approved_issue(981)],
                              events={981: [label_event('labeled', 'jokiefer')]})
        ao = fakes.FakeAO([session('mrmap-77', issue=981, terminated=True)])
        report = controller.tick()
        self.assertIn('NEEDS_HUMAN', report['counts'])
        self.assertTrue(controller.claims_changed)
        self.assertTrue(controller.log.events('board_refresh_failed'))


if __name__ == '__main__':
    unittest.main()
