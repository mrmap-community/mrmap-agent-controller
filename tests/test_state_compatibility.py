"""Compatibility with the running pilot, deployment surface, and logging.

The pilot gatekeeper and its SQLite file must keep working after this controller
is installed, and the controller must not offer a way to merge or approve.
"""
import json
import sqlite3
import unittest
from argparse import Namespace
from contextlib import closing
from pathlib import Path
from tempfile import mkdtemp

from mrmap_controller import cli, config as config_mod, core, db, lifecycle as st
from mrmap_controller import logging_util
from tests import fakes
from tests.fakes import Harness, approved_issue, label_event


def open_db(path):
    """A context manager that really closes.

    ``with sqlite3.connect(...)`` only commits or rolls back, so a test written
    that way leaks a connection and the suite ends in ResourceWarnings.
    """
    return closing(sqlite3.connect(str(path)))

#: Copied from /home/ota/.ao/mrmap-gatekeeper/gatekeeper.py, unchanged. This is
#: the contract the controller must not break.
PILOT_DDL = '''CREATE TABLE IF NOT EXISTS claims (
        issue INTEGER PRIMARY KEY,
        state TEXT NOT NULL,
        session_id TEXT,
        created_at TEXT NOT NULL,
        note TEXT
    )'''


def write_config(directory, state_db, lock_file):
    values = dict(config_mod.DEFAULTS)
    values.update({'state_db': str(state_db), 'lock_file': str(lock_file),
                   'log_dir': str(Path(directory) / 'logs'),
                   'backup_dir': str(Path(directory) / 'backups'),
                   'capacity_script': ''})
    path = Path(directory) / 'config.json'
    path.write_text(json.dumps(values))
    return str(path)


class PilotDatabasePreserved(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(mkdtemp(prefix='mrmap-pilot-'))
        self.path = self.tmp / 'pilot.sqlite3'
        self.lock = self.tmp / 'intake.lock'
        conn = sqlite3.connect(str(self.path))
        conn.execute(PILOT_DDL)
        conn.execute('INSERT INTO claims VALUES(?,?,?,?,?)',
                     (1102, 'spawned', 'mrmap-7', '2026-11-06T11:49:08.370541+00:00',
                      'spawned session mrmap-7 "Issue #1102" (idle)'))
        conn.commit()
        conn.close()
        with open_db(self.path) as conn:
            self.before = list(conn.execute('SELECT * FROM claims'))
            self.columns = [r[1] for r in conn.execute('PRAGMA table_info(claims)')]
        self.config = write_config(self.tmp, self.path, self.lock)

    def rows(self, issue=None):
        with open_db(self.path) as conn:
            if issue is None:
                return [tuple(r) for r in conn.execute(
                    'SELECT * FROM claims ORDER BY issue')]
            return [tuple(r) for r in conn.execute(
                'SELECT * FROM claims WHERE issue=?', (issue,))]

    def test_init_is_additive_only(self):
        conn = db.connect(self.path)
        db.init(conn)
        self.assertEqual([r[1] for r in conn.execute('PRAGMA table_info(claims)')],
                         self.columns)
        self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM claims')],
                         self.before)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'issues', 'dispatches', 'processed_events', 'runs'} <= tables)
        self.assertTrue(db.schema_is_legacy_compatible(conn))
        conn.close()

    def test_the_pilot_query_still_works_after_upgrade(self):
        conn = db.connect(self.path)
        db.init(conn)
        row = conn.execute('SELECT state,session_id FROM claims WHERE issue=?',
                           (1102,)).fetchone()
        self.assertEqual(tuple(row), ('spawned', 'mrmap-7'))
        conn.close()

    def test_incompatible_claims_table_is_refused(self):
        bad = self.tmp / 'bad.sqlite3'
        with open_db(bad) as conn:
            conn.execute('CREATE TABLE claims (issue INTEGER, extra TEXT)')
        conn = db.connect(bad)
        db.init(conn)
        self.assertFalse(db.schema_is_legacy_compatible(conn))
        conn.close()

    def test_a_tick_leaves_an_unrelated_pilot_claim_untouched(self):
        h = Harness(execute=True, tmp=self.tmp)
        h.conn.close()
        h.cfg._v['state_db'] = str(self.path)
        h.conn = db.connect(self.path, durable=False)
        db.init(h.conn)
        gh = fakes.FakeGitHub(
            issues=[approved_issue(130),
                    approved_issue(1102, labels=('agent: approved',))],
            events={130: [label_event('labeled', 'jokiefer')],
                    1102: [label_event('labeled', 'jokiefer')]},
            prs={1103: fakes.pr(1103, 1102, state='OPEN', head='ao/issue-1102/x')})
        # The pilot's worker is still alive in AO, exactly as on the real host.
        ao = fakes.FakeAO([fakes.session('mrmap-7', issue=1102, activity='idle',
                                         prs=[{'number': 1103}])])
        report = h.tick(gh=gh, ao=ao)
        self.assertIn('SPAWNED', report['counts'])
        self.assertEqual(self.rows(1102), self.before,
                         'the pilot claim for a live worker must not move')
        self.assertEqual(len(self.rows()), 2)
        h.close()

    def test_a_claim_whose_session_vanished_is_quarantined_not_restarted(self):
        h = Harness(execute=True, tmp=self.tmp)
        h.conn.close()
        h.cfg._v['state_db'] = str(self.path)
        h.conn = db.connect(self.path, durable=False)
        db.init(h.conn)
        report = h.tick(gh=fakes.FakeGitHub(), ao=fakes.FakeAO([]))
        self.assertIn('NEEDS_HUMAN', report['counts'])
        row = self.rows(1102)[0]
        self.assertEqual(row[0], 1102)          # the row itself is never deleted
        self.assertEqual(row[2], 'mrmap-7')     # its session association survives
        self.assertEqual(row[1], db.CLAIM_RECONCILE)
        h.close()

    def test_reconciliation_stops_when_ao_cannot_be_read(self):
        h = Harness(execute=True, tmp=self.tmp)
        h.conn.close()
        h.cfg._v['state_db'] = str(self.path)
        h.conn = db.connect(self.path, durable=False)
        db.init(h.conn)
        ao = fakes.FakeAO([])
        ao.fail_on.add('sessions')
        h.tick(gh=fakes.FakeGitHub(), ao=ao)
        self.assertEqual(self.rows(1102)[0][1], 'spawned',
                         'a failed AO read must not quarantine anything')
        self.assertTrue(h.log.events('reconciliation_skipped'))
        h.close()

    def test_backup_is_consistent_and_private(self):
        code = cli.cmd_backup(Namespace(config=self.config, dest=str(self.tmp / 'bk')))
        self.assertEqual(code, 0)
        copies = sorted((self.tmp / 'bk').glob('state-*.sqlite3'))
        self.assertEqual(len(copies), 1)
        with open_db(copies[0]) as conn:
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM claims')],
                         self.before)
        self.assertEqual(copies[0].stat().st_mode & 0o077, 0,
                         'backups must not be world readable')


class DeploymentSurface(unittest.TestCase):
    def test_no_subcommand_can_approve_merge_or_resolve(self):
        parser = cli.build_parser()
        commands = set()
        for action in parser._subparsers._group_actions:
            commands |= set(action.choices or {})
        self.assertTrue({'tick', 'status', 'logs', 'doctor', 'backup', 'release',
                         'pause', 'resume', 'loop', 'start', 'stop'} <= commands)
        for forbidden in ('merge', 'approve', 'resolve', 'close', 'label', 'kill',
                          'prune'):
            self.assertNotIn(forbidden, commands)
        source = Path(cli.__file__).read_text()
        for forbidden in ('pr merge', 'pr review', 'resolve-comments', 'session kill',
                          'gh issue edit'):
            self.assertNotIn(forbidden, source)

    def test_release_needs_an_explicit_confirm(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        h.conn.close()
        h.cfg._v['state_db'] = str(Path(h.tmp) / 'pilot2.sqlite3')
        h.conn = db.connect(h.cfg.state_db, durable=False)
        db.init(h.conn)
        h.claim(131, state='needs_reconciliation', session_id='mrmap-9')
        config = write_config(h.tmp, h.cfg.state_db, h.cfg.lock_file)
        code = cli.cmd_release(Namespace(config=config, issue=131, confirm=False,
                                         note=None, who='tester'))
        self.assertEqual(code, 2)
        self.assertEqual(db.get_claim(h.conn, 131)['state'], 'needs_reconciliation')
        self.assertEqual(cli.cmd_release(Namespace(config=config, issue=131,
                                                   confirm=True, note='checked',
                                                   who='tester')), 0)
        self.assertEqual(db.get_claim(h.conn, 131)['state'], db.CLAIM_RELEASED)

    def test_lock_prevents_overlapping_ticks(self):
        h = Harness(execute=True)
        self.addCleanup(h.close)
        cfg, log = cli.build(Namespace(config=write_config(h.tmp, h.cfg.state_db,
                                                           h.cfg.lock_file),
                                       verbose=False))
        first = cli.exclusive_lock(cfg, log)
        self.assertIsNotNone(first)
        self.assertIsNone(cli.exclusive_lock(cfg, log),
                          'a second tick must not run beside the first')
        first.close()
        third = cli.exclusive_lock(cfg, log)
        try:
            self.assertIsNotNone(third,
                                 'the lock must be free again after the tick ends')
        finally:
            if third is not None:
                third.close()

    def test_pause_and_resume_toggle_the_marker(self):
        original = cli.PAUSE_MARKER
        cli.PAUSE_MARKER = Path(mkdtemp(prefix='mrmap-pause-')) / 'paused'
        cli.PAUSE_MARKER.parent.mkdir(exist_ok=True)
        try:
            self.assertEqual(cli.cmd_pause(Namespace(reason='test')), 0)
            self.assertTrue(cli.PAUSE_MARKER.exists())
            self.assertEqual(cli.cmd_resume(Namespace()), 0)
            self.assertFalse(cli.PAUSE_MARKER.exists())
        finally:
            cli.PAUSE_MARKER = original

    def test_units_poll_every_five_minutes_and_only_run_the_tick(self):
        systemd = Path(cli.INSTALL) / 'systemd'
        service = (systemd / 'mrmap-controller.service').read_text()
        timer = (systemd / 'mrmap-controller.timer').read_text()
        self.assertIn('Type=oneshot', service)
        self.assertIn('tick --execute', service)
        self.assertIn('UMask=0077', service)
        self.assertIn('OnUnitActiveSec=5min', timer)
        self.assertIn('Persistent=true', timer)
        # comments may say what the controller never does; command lines must not
        commands = [line for line in (service + timer).splitlines()
                    if line.split('=') and line.split('=')[0].rstrip().endswith(
                        ('ExecStart', 'ExecStop', 'ExecStartPre', 'ExecStopPost'))]
        self.assertTrue(commands)
        for line in commands:
            for forbidden in ('merge', 'approve', 'prune', 'rm ', 'kill'):
                self.assertNotIn(forbidden, line, f'{forbidden} in unit command: {line}')

    def test_install_script_resolves_the_same_install_dir_as_the_entry_point(self):
        # a wrong INSTALL makes install.sh write config and units somewhere else
        script = (Path(cli.INSTALL) / 'install.sh').read_text()
        line = next(l for l in script.splitlines() if l.startswith('INSTALL='))
        self.assertNotIn('/.." ', line + ' ', f'{line} points above the install root')
        self.assertIn('dirname "$SELF"', line)

    def test_install_script_enables_only_behind_an_explicit_flag(self):
        script = (Path(cli.INSTALL) / 'install.sh').read_text()
        self.assertIn('mrmap-controller\" backup', script,
                      'installation must back up the state database first')
        self.assertIn('ao automation', script,
                      'installation must check for a competing AO scheduler')
        self.assertIn('ENABLE=0', script, 'a fresh install must stay disabled')
        guard = script.index('if [[ $ENABLE -eq 0 ]]')
        enabler = script.index('systemctl --user enable --now')
        self.assertLess(guard, enabler,
                        'enabling must sit behind the --enable branch only')
        self.assertNotIn('enable --now mrmap-controller.timer &&', script)


class StructuredLogging(unittest.TestCase):
    def test_events_are_json_with_redaction(self):
        log_dir = Path(mkdtemp(prefix='mrmap-log-'))
        log = logging_util.setup(log_dir)
        log.warning('DELIVERY_FAILED', issue=120, pr=500, session='mrmap-7',
                    reason='token ghp_' + 'A' * 36 + ' leaked here')
        line = (log_dir / 'controller.jsonl').read_text().strip().splitlines()[-1]
        record = json.loads(line)
        self.assertEqual(record['event'], 'DELIVERY_FAILED')
        self.assertEqual(record['issue'], 120)
        self.assertEqual(record['level'], 'WARNING')
        self.assertNotIn('ghp_', record['reason'])
        self.assertIn('[redacted]', record['reason'])

    def test_log_file_rotates(self):
        import logging
        log_dir = Path(mkdtemp(prefix='mrmap-log2-'))
        log = logging_util.setup(log_dir)
        for index in range(3):
            log.info('ROTATION_PROBE', detail=str(index) * 1000)
        self.assertTrue((log_dir / 'controller.jsonl').exists())
        handlers = logging.getLogger('mrmap-controller').handlers
        self.assertTrue(any(isinstance(h, logging.handlers.RotatingFileHandler)
                            for h in handlers))


class LifecycleVocabulary(unittest.TestCase):
    def test_documented_states_exist(self):
        for name in ('queued', 'running', 'pr_open', 'awaiting_ci', 'fixing_ci',
                     'awaiting_review', 'addressing_review', 'ready_for_human_merge',
                     'merged', 'failed', 'paused'):
            self.assertIn(name, st.ALL)

    def test_no_state_implies_an_action_by_the_controller(self):
        for invented in ('merged_by_controller', 'approved_by_controller', 'closed'):
            self.assertNotIn(invented, st.ALL)

    def test_every_state_is_produced_or_documented(self):
        # states that no code path can reach are documentation debt
        import pathlib
        source = pathlib.Path(st.__file__).with_name('core.py').read_text()
        for name in st.ALL:
            self.assertIn(f'st.{name.upper()}', source, f'{name} is never assigned')


class AgainstTheRealPilot(unittest.TestCase):
    """Run the pilot's own code against a database the controller has migrated.

    Skipped on a host without the pilot; on the deployment host it is the proof
    that installing the controller does not retire the gatekeeper.
    """

    PILOT = Path('/home/ota/.ao/mrmap-gatekeeper/gatekeeper.py')

    def setUp(self):
        import importlib.util
        if not self.PILOT.exists():
            self.skipTest(f'no pilot at {self.PILOT}')
        spec = importlib.util.spec_from_file_location('pilot_gatekeeper', self.PILOT)
        self.pilot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.pilot)
        self.tmp = Path(mkdtemp(prefix='mrmap-realpilot-'))
        self.path = self.tmp / 'state.sqlite3'
        conn = sqlite3.connect(str(self.path))
        conn.execute(PILOT_DDL)
        conn.execute('INSERT INTO claims VALUES(?,?,?,?,?)',
                     (1102, 'spawned', 'mrmap-7', '2026-11-06T11:49:08+00:00', 'pilot claim'))
        conn.commit()
        conn.close()

    def test_pilot_can_read_and_write_a_migrated_database(self):
        conn = db.connect(self.path)
        db.init(conn)
        db.set_claim(conn, 1102, db.CLAIM_MERGED, '2026-11-06T15:00:00+00:00', note='controller')
        db.claim_issue(conn, 1150, db.CLAIM_RECONCILE, '2026-11-06T15:00:00+00:00',
                       'controller claim')
        mode_before = conn.execute('PRAGMA journal_mode').fetchone()[0]
        conn.close()

        pilot_conn = sqlite3.connect(str(self.path))
        try:
            self.pilot.init_db(pilot_conn)          # the pilot's own schema init
            rows = [dict(zip([d[0] for d in cur.description], r))
                    for cur in [pilot_conn.execute('SELECT * FROM claims')] for r in cur]
            self.assertEqual({r['issue'] for r in rows}, {1102, 1150})
            # the pilot counts workers from AO, never from a claim state it has not seen
            self.assertEqual(self.pilot.active_workers(
                [{'role': 'worker', 'isTerminated': False}]),
                [{'role': 'worker', 'isTerminated': False}])
            pilot_conn.execute('UPDATE claims SET note=? WHERE issue=?', ('pilot wrote', 1102))
            pilot_conn.commit()
            self.assertEqual(
                pilot_conn.execute('SELECT note FROM claims WHERE issue=1102').fetchone()[0],
                'pilot wrote')
            # and the controller never switched the file out from under it
            self.assertEqual(pilot_conn.execute('PRAGMA journal_mode').fetchone()[0],
                             mode_before)
        finally:
            pilot_conn.close()

    def test_pilot_claim_columns_are_the_ones_the_controller_writes(self):
        with open_db(self.path) as conn:
            self.assertEqual(list(db.CLAIM_COLUMNS),
                             [r[1] for r in conn.execute('PRAGMA table_info(claims)')])

    def test_a_released_claim_is_free_for_the_controller_and_still_held_for_the_pilot(self):
        # Deliberate divergence, documented in OPERATIONS.md: the row stays for the
        # audit trail. The controller reads "released" as free, the pilot reads any
        # row as held. Both are safe; only one of them is ever running.
        conn = db.connect(self.path)
        db.init(conn)
        db.release_claim(conn, 1102, '2026-11-06T15:00:00+00:00', 'operator released it')
        issue = {'number': 1102, 'state': 'open', 'labels': [{'name': 'agent: approved'}]}
        ok, reason = self.pilot.inspect(issue, [], conn)
        self.assertFalse(ok)
        self.assertIn('Already claimed (released', reason)
        self.assertFalse(core.claim_holds(db.get_claim(conn, 1102)['state']))
        conn.close()


if __name__ == '__main__':
    unittest.main()
