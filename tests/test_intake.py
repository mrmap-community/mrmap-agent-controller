"""Phase 1: issue intake, human approval verification, no duplicates.

Every test drives a real :class:`Controller` against fakes and a throwaway
database in a temporary directory. Nothing here reaches GitHub, AO or the
production state file.
"""
import unittest

from mrmap_controller import db
from tests import fakes
from tests.fakes import (Harness, approved_issue, label_event, open_pr_entry, session)

APPROVED_EVENTS = {120: [label_event('labeled', 'jokiefer')]}


def gh_one(events=None, issues=None, pulls=None):
    return fakes.FakeGitHub(
        issues=issues if issues is not None else [approved_issue(120)],
        events=events if events is not None else dict(APPROVED_EVENTS),
        pulls=pulls or [])


class ApprovalVerification(unittest.TestCase):
    def setUp(self):
        self.h = Harness(execute=True)

    def tearDown(self):
        self.h.close()

    def test_unauthorized_approval_is_rejected(self):
        report = self.h.tick(gh=gh_one({120: [label_event('labeled', 'somebody-else')]}))
        self.assertIn('REJECT', report['counts'])
        self.assertIn('not an allowlisted human', fakes.reasons(report, 'REJECT'))
        self.assertEqual(self.h.ao.count('spawn'), 0)

    def test_bot_approval_is_rejected(self):
        report = self.h.tick(gh=gh_one({120: [label_event('labeled', 'mrmap-agent-bot')]}))
        self.assertIn('REJECT', report['counts'])
        self.assertEqual(self.h.ao.count('spawn'), 0)

    def test_human_approval_is_accepted_and_spawns(self):
        report = self.h.tick(gh=gh_one())
        self.assertIn('SPAWNED', report['counts'])
        self.assertEqual(self.h.ao.count('spawn'), 1)
        claim = db.get_claim(self.h.conn, 120)
        self.assertEqual(claim['state'], db.CLAIM_SPAWNED)
        self.assertTrue(claim['session_id'].startswith('mrmap-'))

    def test_removed_approval_is_rejected(self):
        report = self.h.tick(gh=gh_one({120: [
            label_event('labeled', 'jokiefer', event_id=1),
            label_event('unlabeled', 'jokiefer', event_id=2)]}))
        self.assertIn('REJECT', report['counts'])
        self.assertIn('removed', fakes.reasons(report, 'REJECT'))
        self.assertEqual(self.h.ao.count('spawn'), 0)

    def test_relabel_by_an_allowlisted_human_after_removal_is_accepted(self):
        report = self.h.tick(gh=gh_one({120: [
            label_event('labeled', 'jokiefer', event_id=1),
            label_event('unlabeled', 'jokiefer', event_id=2),
            label_event('labeled', 'jokiefer', event_id=3)]}))
        self.assertIn('SPAWNED', report['counts'])

    def test_issue_without_any_label_event_is_rejected(self):
        report = self.h.tick(gh=gh_one({120: []}))
        self.assertIn('REJECT', report['counts'])
        self.assertEqual(self.h.ao.count('spawn'), 0)

    def test_paused_issue_is_skipped(self):
        issue = approved_issue(120, labels=('agent: approved', 'agent: paused'))
        report = self.h.tick(gh=gh_one(issues=[issue]))
        self.assertIn('SKIP', report['counts'])
        self.assertIn('paused', fakes.reasons(report, 'SKIP'))
        self.assertEqual(self.h.rows()[120]['state'], 'paused')
        self.assertEqual(self.h.ao.count('spawn'), 0)

    def test_closed_issue_is_not_listed_by_the_label_query(self):
        report = self.h.tick(gh=gh_one(issues=[approved_issue(120, state='closed')]))
        self.assertEqual(report['counts'], {})
        self.assertEqual(self.h.ao.count('spawn'), 0)


class DuplicateProtection(unittest.TestCase):
    def setUp(self):
        self.h = Harness(execute=True)

    def tearDown(self):
        self.h.close()

    def test_existing_claim_is_never_assigned_twice(self):
        self.h.claim(120, state='spawned', session_id='mrmap-7')
        report = self.h.tick(gh=gh_one())
        self.assertIn('SKIP', report['counts'])
        self.assertIn('already claimed', fakes.reasons(report, 'SKIP'))
        self.assertEqual(self.h.ao.count('spawn'), 0)

    def test_existing_ao_worker_is_detected_by_issue_id(self):
        report = self.h.tick(gh=gh_one(),
                             ao=fakes.FakeAO([session('mrmap-9', issue=120)]))
        self.assertIn('SKIP', report['counts'])
        self.assertIn('already carries this issueId', fakes.reasons(report, 'SKIP'))
        self.assertEqual(self.h.ao.count('spawn'), 0)
        self.assertEqual(db.get_claim(self.h.conn, 120)['session_id'], 'mrmap-9')

    def test_dry_run_does_not_write_the_assignment_board(self):
        report = self.h.tick(execute=False, gh=gh_one(),
                             ao=fakes.FakeAO([session('mrmap-9', issue=120)]))
        self.assertIn('SKIP', report['counts'])
        self.assertIsNone(db.get_claim(self.h.conn, 120))

    def test_terminated_session_is_reported_not_respawned(self):
        self.h.claim(120, state='spawned', session_id='mrmap-9')
        ao = fakes.FakeAO([session('mrmap-9', issue=120, activity='exited',
                                   terminated=True)])
        report = self.h.tick(gh=gh_one(), ao=ao)
        self.assertIn('NEEDS_HUMAN', report['counts'])
        self.assertIn('terminated', fakes.reasons(report, 'NEEDS_HUMAN'))
        self.assertEqual(ao.count('spawn'), 0)
        self.assertEqual(ao.count('resume_agent'), 0)

    def test_existing_open_pr_is_detected(self):
        report = self.h.tick(gh=gh_one(pulls=[open_pr_entry(500, 120)]))
        self.assertIn('SKIP', report['counts'])
        self.assertIn('already references this issue', fakes.reasons(report, 'SKIP'))
        self.assertEqual(self.h.ao.count('spawn'), 0)

    def test_pr_reference_needs_a_full_number_match(self):
        # A PR about #1203 must not be taken as a reference to issue #120.
        report = self.h.tick(gh=gh_one(pulls=[open_pr_entry(
            500, title='fix (#1203)', head='ao/feature/unrelated')]))
        self.assertIn('SPAWNED', report['counts'])

    def test_duplicate_claim_insert_loses_the_race(self):
        self.assertTrue(db.claim_issue(self.h.conn, 130, 'spawning', 'now', 'first'))
        self.assertFalse(db.claim_issue(self.h.conn, 130, 'spawning', 'now', 'second'))

    def test_released_claim_allows_a_new_assignment(self):
        self.h.claim(120, state='needs_reconciliation')
        db.release_claim(self.h.conn, 120, 'now', 'operator checked it')
        report = self.h.tick(gh=gh_one())
        self.assertIn('SPAWNED', report['counts'])


class CrashRecovery(unittest.TestCase):
    def setUp(self):
        self.h = Harness(execute=True)

    def tearDown(self):
        self.h.close()

    def test_crash_during_spawn_adopts_the_live_session(self):
        self.h.claim(118, state='spawning', session_id=None, age_minutes=1)
        ao = fakes.FakeAO([session('mrmap-77', issue=118, activity='idle')])
        report = self.h.tick(gh=fakes.FakeGitHub(issues=[approved_issue(118)]), ao=ao)
        self.assertIn('ADOPTED', report['counts'])
        self.assertEqual(ao.count('spawn'), 0)
        claim = db.get_claim(self.h.conn, 118)
        self.assertEqual((claim['state'], claim['session_id']),
                         (db.CLAIM_SPAWNED, 'mrmap-77'))
        self.assertEqual(self.h.rows()[118]['session_id'], 'mrmap-77')

    def test_claim_without_session_is_quarantined_after_grace(self):
        self.h.claim(119, state='spawned', session_id='mrmap-70', age_minutes=90)
        report = self.h.tick(gh=fakes.FakeGitHub(), ao=fakes.FakeAO([]))
        self.assertIn('NEEDS_HUMAN', report['counts'])
        self.assertEqual(db.get_claim(self.h.conn, 119)['state'], db.CLAIM_RECONCILE)
        self.assertEqual(self.h.rows()[119]['state'], 'failed')

    def test_recent_claim_without_session_is_still_pending(self):
        self.h.claim(119, state='spawning', session_id=None, age_minutes=2)
        report = self.h.tick(gh=fakes.FakeGitHub(), ao=fakes.FakeAO([]))
        self.assertIn('PENDING_SPAWN', report['counts'])
        self.assertEqual(db.get_claim(self.h.conn, 119)['state'], db.CLAIM_SPAWNING)

    def test_spawn_timeout_keeps_the_claim_and_does_not_retry(self):
        ao = fakes.FakeAO([])
        ao.fail_on.add('spawn')
        report = self.h.tick(gh=gh_one(), ao=ao)
        self.assertIn('SPAWN_UNCERTAIN', report['counts'])
        self.assertEqual(ao.count('spawn'), 1)
        self.assertEqual(db.get_claim(self.h.conn, 120)['state'], db.CLAIM_SPAWNING)

    def test_successful_spawn_without_visible_session_stays_uncertain(self):
        ao = fakes.FakeAO([])
        ao.spawn_creates = False
        report = self.h.tick(gh=gh_one(), ao=ao)
        self.assertIn('SPAWN_UNCERTAIN', report['counts'])
        self.assertEqual(ao.count('spawn'), 1)
        self.assertEqual(db.get_claim(self.h.conn, 120)['state'], db.CLAIM_SPAWNING)

    def test_two_sessions_for_one_issue_are_never_resolved_by_guessing(self):
        self.h.claim(118, state='spawning', session_id=None, age_minutes=1)
        ao = fakes.FakeAO([session('mrmap-81', issue=118), session('mrmap-82', issue=128)])
        report = self.h.tick(gh=fakes.FakeGitHub(issues=[approved_issue(118)]), ao=ao)
        self.assertNotIn('SPAWNED', report['counts'])
        self.assertEqual(ao.count('spawn'), 0)


class FailClosed(unittest.TestCase):
    def setUp(self):
        self.h = Harness(execute=True)

    def tearDown(self):
        self.h.close()

    def test_uncertain_approval_event_leads_to_rejection(self):
        gh = gh_one()
        gh.fail_on.add('issue_label_events')
        report = self.h.tick(gh=gh)
        self.assertIn('cannot verify approval label', fakes.reasons(report, 'REJECT'))
        self.assertEqual(self.h.ao.count('spawn'), 0)

    def test_uncertain_pr_listing_blocks_intake(self):
        gh = gh_one()
        gh.fail_on.add('open_pulls')
        report = self.h.tick(gh=gh)
        self.assertIn('GitHub state uncertain', fakes.reasons(report, 'SKIP'))
        self.assertEqual(self.h.ao.count('spawn'), 0)

    def test_uncertain_ao_state_blocks_intake(self):
        ao = fakes.FakeAO([])
        ao.fail_on.add('sessions')
        report = self.h.tick(gh=gh_one(), ao=ao)
        self.assertIn('AO state uncertain', fakes.reasons(report, 'SKIP'))
        self.assertEqual(ao.count('spawn'), 0)

    def test_labels_changed_before_spawn_abort_the_start(self):
        gh = gh_one()
        # The listing the tick started from still shows the issue approved; the
        # last-moment re-check inside _spawn sees the pause.
        gh.issue = lambda number: approved_issue(
            120, labels=('agent: approved', 'agent: paused'))
        report = self.h.tick(gh=gh)
        self.assertIn('labels changed before spawn', fakes.reasons(report, 'SKIP'))
        self.assertEqual(self.h.ao.count('spawn'), 0)
        self.assertEqual(db.get_claim(self.h.conn, 120)['state'], db.CLAIM_RELEASED)

    def test_blocking_relationships_are_never_read(self):
        gh = gh_one()
        gh.blocking_by_issue = {120: [119]}        # open, unfinished blocker
        report = self.h.tick(gh=gh)
        self.assertIn('SPAWNED', report['counts'])
        for name in gh.names():
            for forbidden in ('block', 'depend', 'sub_issue', 'linked'):
                self.assertNotIn(forbidden, name.lower())


class CapacityAndMode(unittest.TestCase):
    def setUp(self):
        self.h = Harness(execute=True)

    def tearDown(self):
        self.h.close()

    def test_worker_limit_is_enforced(self):
        ao = fakes.FakeAO([session('mrmap-50', issue=111, activity='active')])
        report = self.h.tick(gh=gh_one(), ao=ao)
        self.assertIn('DEFER', report['counts'])
        self.assertIn('worker limit reached', fakes.reasons(report, 'DEFER'))
        self.assertEqual(ao.count('spawn'), 0)

    def test_idle_workers_do_not_consume_a_slot(self):
        ao = fakes.FakeAO([session('mrmap-50', issue=111, activity='idle')])
        report = self.h.tick(gh=gh_one(), ao=ao)
        self.assertIn('SPAWNED', report['counts'])

    def test_three_workers_allowed_when_configured(self):
        self.h.cfg._v['max_workers'] = 3
        gh = fakes.FakeGitHub(
            issues=[approved_issue(120), approved_issue(121), approved_issue(122)],
            events={n: [label_event('labeled', 'jokiefer')] for n in (120, 121, 122)})
        report = self.h.tick(gh=gh, ao=fakes.FakeAO([]))
        self.assertEqual(report['counts'].get('SPAWNED', 0), 3)

    def test_fourth_worker_is_still_blocked_at_a_limit_of_three(self):
        self.h.cfg._v['max_workers'] = 3
        gh = fakes.FakeGitHub(
            issues=[approved_issue(n) for n in (120, 121, 122, 123)],
            events={n: [label_event('labeled', 'jokiefer')]
                    for n in (120, 121, 122, 123)})
        report = self.h.tick(gh=gh, ao=fakes.FakeAO([]))
        self.assertEqual(report['counts'].get('SPAWNED', 0), 3)
        self.assertEqual(report['counts'].get('DEFER', 0), 1)

    def test_worker_limit_above_three_is_refused(self):
        values = dict(fakes.config_mod.DEFAULTS)
        values['max_workers'] = 5
        with self.assertRaises(fakes.config_mod.ConfigError):
            fakes.config_mod._validate(fakes.config_mod.Config(values))

    def test_host_capacity_gate_can_block_a_spawn(self):
        self.h.capacity_ok = False
        report = self.h.tick(gh=gh_one(), ao=fakes.FakeAO([]))
        self.assertIn('host capacity gate rejected', fakes.reasons(report, 'DEFER'))
        self.assertEqual(self.h.ao.count('spawn'), 0)

    def test_dry_run_starts_nothing(self):
        ao = fakes.FakeAO([])
        report = self.h.tick(execute=False, gh=gh_one(), ao=ao)
        self.assertIn('WOULD_SPAWN', report['counts'])
        self.assertEqual(ao.count('spawn'), 0)
        self.assertIsNone(db.get_claim(self.h.conn, 120))

    def test_pause_marker_makes_execution_report_only(self):
        self.h.pause_file.write_text('maintenance\n')
        ao = fakes.FakeAO([])
        self.h.tick(gh=gh_one(), ao=ao)
        self.assertEqual(ao.count('spawn'), 0)

    def test_prompt_carries_the_docker_wrapper_and_the_never_merge_rule(self):
        ao = fakes.FakeAO([])
        self.h.tick(gh=gh_one(), ao=ao)
        prompt = ao.with_name('spawn')[0]['prompt']
        self.assertIn('/home/ota/.ao/bin/mrmap-docker-test', prompt)
        self.assertIn('Never approve, merge', prompt)
        self.assertIn('untrusted', prompt.lower())


if __name__ == '__main__':
    unittest.main()
