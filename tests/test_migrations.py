"""Schema integrity (W1): a legacy database migrates additively or is refused.

The live state database was created 2026-11-06 by the legacy pilot; its
``dispatches`` table has no ``attempts`` column, and every delivery path then
aborted with ``sqlite3.OperationalError: no such column: attempts`` -- 456
ticks, 35.6 h of it unbroken (incident report A §A.1, root cause B-3.1).
These tests rebuild that legacy shape verbatim and pin the repair: additive
``ALTER TABLE ... ADD COLUMN`` only, never a drop or recreate, idempotent
re-inits, full pilot compatibility, and a hard refusal of files stamped with a
newer layout.
"""
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkdtemp

from mrmap_controller import db

#: Copied verbatim from the live /home/ota/.ao/mrmap-gatekeeper/state.sqlite3
#: sqlite_master (2026-11-09): the table that broke every delivery.
LEGACY_DISPATCHES_DDL = '''CREATE TABLE dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    issue INTEGER NOT NULL,
    pr_number INTEGER,
    dedupe_key TEXT NOT NULL UNIQUE,
    head_sha TEXT,
    session_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    detail TEXT
)'''

#: Copied verbatim from the pilot's gatekeeper.py (contract, not drift).
PILOT_CLAIMS_DDL = '''CREATE TABLE claims (
    issue INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    note TEXT
)'''

LEGACY_ROW = (1, 'ci', 981, 1194, f'ci:1194:{"a" * 40}', 'a' * 40, 'mrmap-77',
              'pending', '2026-11-06T18:37:23+00:00',
              'row written by the pilot-era table shape')


class LegacyDispatchesMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(mkdtemp(prefix='mrmap-migration-'))
        self.path = self.tmp / 'state.sqlite3'
        raw = sqlite3.connect(str(self.path))
        raw.execute(PILOT_CLAIMS_DDL)
        raw.execute(LEGACY_DISPATCHES_DDL)
        raw.execute('INSERT INTO claims VALUES(?,?,?,?,?)',
                    (981, 'spawned', 'mrmap-77', '2026-11-06T11:49:08+00:00',
                     'pilot claim'))
        raw.execute('INSERT INTO dispatches VALUES(?,?,?,?,?,?,?,?,?,?)',
                    LEGACY_ROW)
        raw.commit()
        raw.close()
        self.conn = db.connect(self.path, durable=False)

    def tearDown(self):
        self.conn.close()

    def test_init_adds_the_missing_columns_and_stamps_the_layout(self):
        db.init(self.conn)
        cols = db.columns_of(self.conn, 'dispatches')
        self.assertIn('attempts', cols, 'the incident column is still missing')
        self.assertIn('next_retry_at', cols)
        self.assertEqual(self.conn.execute('PRAGMA user_version').fetchone()[0],
                         db.SCHEMA_VERSION)
        self.assertTrue(db.schema_is_legacy_compatible(self.conn))

    def test_ensure_schema_reports_exactly_the_additions(self):
        # init() runs ensure_schema after CREATE IF NOT EXISTS; call the
        # function alone on the untouched legacy file for its return value.
        added = db.ensure_schema(self.conn)
        self.assertEqual(added, ['dispatches.attempts',
                                 'dispatches.next_retry_at'])
        self.assertEqual(db.ensure_schema(self.conn), [],
                         'a repaired file repairs nothing a second time')

    def test_attempt_count_works_again_after_the_migration(self):
        db.init(self.conn)
        # the incident query: SUM over the column that did not exist before
        self.assertEqual(self.conn.execute(
            'SELECT COALESCE(SUM(attempts),0) FROM dispatches WHERE pr_number=? '
            "AND kind IN ('ci','combined')", (1194,)).fetchone()[0], 1)
        self.assertEqual(db.attempt_count(self.conn, 1194, ('ci', 'combined')), 1)
        db.bump_dispatch(self.conn, LEGACY_ROW[4])
        self.assertEqual(db.attempt_count(self.conn, 1194, ('ci', 'combined')), 2)

    def test_new_delivery_on_a_migrated_legacy_table_works(self):
        db.init(self.conn)
        key = f'ci:1200:{"b" * 40}'
        row_id = db.try_reserve_dispatch(self.conn, 'ci', 1200, key,
                                         '2026-11-09T09:00:00+00:00',
                                         pr_number=1200, detail='after repair')
        self.assertIsNotNone(row_id)
        self.assertEqual(db.dispatch_for(self.conn, key)['status'], 'pending')
        self.assertEqual(db.attempt_count(self.conn, 1200, ('ci', 'combined')), 1)

    def test_existing_legacy_row_survives_untouched(self):
        db.init(self.conn)
        row = db.dispatch_for(self.conn, LEGACY_ROW[4])
        self.assertEqual((row['kind'], row['status'], row['issue']),
                         ('ci', 'pending', 981))
        self.assertEqual(row['attempts'], 1, 'existing rows get the column default')
        self.assertIsNone(row['next_retry_at'])

    def test_second_init_is_a_no_op(self):
        db.init(self.conn)
        schema_before = [(r['type'], r['name'], r['sql']) for r in self.conn.execute(
            'SELECT type, name, sql FROM sqlite_master ORDER BY type, name')]
        claims_before = self.conn.execute('SELECT * FROM claims').fetchall()
        db.init(self.conn)
        self.assertEqual([(r['type'], r['name'], r['sql']) for r in self.conn.execute(
            'SELECT type, name, sql FROM sqlite_master ORDER BY type, name')],
            schema_before)
        self.assertEqual(self.conn.execute('SELECT * FROM claims').fetchall(),
                         claims_before)

    def test_migrating_never_drops_or_recreates_anything(self):
        raw = sqlite3.connect(str(self.path))
        # a column and an index this controller does not know about
        raw.execute('ALTER TABLE dispatches ADD COLUMN foreign_audit TEXT')
        raw.execute('CREATE INDEX legacy_idx ON dispatches(status)')
        raw.execute('UPDATE dispatches SET foreign_audit=? WHERE id=1', ('keep me',))
        raw.commit()
        raw.close()
        db.init(self.conn)
        cols = db.columns_of(self.conn, 'dispatches')
        self.assertIn('foreign_audit', cols, 'additive-only means additive-only')
        idx = {r['name'] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='dispatches'")}
        self.assertIn('legacy_idx', idx,
                      'recreating the table would have silently dropped it')
        self.assertEqual(self.conn.execute(
            'SELECT foreign_audit FROM dispatches WHERE id=1').fetchone()[0],
            'keep me')

    def test_pilot_only_database_passes(self):
        path = self.tmp / 'pilot-only.sqlite3'
        raw = sqlite3.connect(str(path))
        raw.execute(PILOT_CLAIMS_DDL)
        raw.execute('INSERT INTO claims VALUES(?,?,?,?,?)',
                    (1102, 'spawned', 'mrmap-7', '2026-11-06T11:49:08+00:00', 'pilot'))
        raw.commit()
        raw.close()
        conn = db.connect(path, durable=False)
        db.init(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'claims', 'issues', 'dispatches', 'processed_events',
                         'runs'} <= tables)
        self.assertTrue(db.schema_is_legacy_compatible(conn))
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM claims').fetchone()[0], 1)
        self.assertEqual(db.ensure_schema(conn), [])
        conn.close()

    def test_a_file_stamped_newer_than_this_code_is_refused(self):
        db.init(self.conn)
        self.conn.execute('PRAGMA user_version=999')
        self.conn.commit()
        with self.assertRaises(db.SchemaVersionError):
            db.ensure_schema(self.conn)
        # init() must refuse the same file, loudly, before using it
        raw = sqlite3.connect(str(self.path))
        raw.execute('PRAGMA user_version=999')
        raw.commit()
        raw.close()
        conn = db.connect(self.path, durable=False)
        with self.assertRaises(db.SchemaVersionError):
            db.init(conn)
        conn.close()

    def test_migrated_legacy_file_matches_a_fresh_install_shape(self):
        db.init(self.conn)
        fresh = db.connect(self.tmp / 'fresh.sqlite3', durable=False)
        db.init(fresh)
        for table in ('claims', 'issues', 'dispatches', 'processed_events', 'runs'):
            self.assertEqual(sorted(db.columns_of(self.conn, table)),
                             sorted(db.columns_of(fresh, table)),
                             f'{table}: migrated legacy and fresh shape differ')
        fresh.close()

    def test_partial_drift_only_gets_the_missing_piece(self):
        # a file where someone already added one column: the repair keys on
        # PRAGMA, not on the stamp alone
        raw = sqlite3.connect(str(self.path))
        raw.execute('ALTER TABLE dispatches ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1')
        raw.commit()
        raw.close()
        added = db.ensure_schema(self.conn)
        self.assertEqual(added, ['dispatches.next_retry_at'])
        self.assertEqual(self.conn.execute('PRAGMA user_version').fetchone()[0],
                         db.SCHEMA_VERSION)


class NextRetryRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(mkdtemp(prefix='mrmap-backoffcol-'))
        self.conn = db.connect(self.tmp / 'state.sqlite3', durable=False)
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_next_retry_at_is_set_and_cleared_with_the_outcome(self):
        db.try_reserve_dispatch(self.conn, 'ci', 120, 'ci:500:sha',
                                '2026-11-09T09:00:00+00:00', pr_number=500)
        db.set_dispatch(self.conn, 'ci:500:sha', 'failed',
                        next_retry_at='2026-11-09T09:05:00+00:00')
        self.assertEqual(db.dispatch_for(self.conn, 'ci:500:sha')['next_retry_at'],
                         '2026-11-09T09:05:00+00:00')
        db.set_dispatch(self.conn, 'ci:500:sha', 'dispatched')
        self.assertIsNone(db.dispatch_for(self.conn, 'ci:500:sha')['next_retry_at'],
                          'a delivered dispatch carries no retry gate')


class AbortStreak(unittest.TestCase):
    """The runs-ledger query behind the watchdog (W4)."""

    def setUp(self):
        self.tmp = Path(mkdtemp(prefix='mrmap-streak-'))
        self.conn = db.connect(self.tmp / 'state.sqlite3', durable=False)
        db.init(self.conn)
        self.ids = []

    def tearDown(self):
        self.conn.close()

    def run_with(self, outcome):
        rid = db.start_run(self.conn, 'execute', '2026-11-09T09:00:00+00:00')
        self.ids.append(rid)
        db.finish_run(self.conn, rid, '2026-11-09T09:00:05+00:00', outcome)
        return rid

    def test_empty_ledger_is_zero(self):
        self.assertEqual(db.abort_streak(self.conn), (0, 0))

    def test_counts_the_run_after_the_last_non_aborted(self):
        ok = self.run_with('ok')
        for _ in range(3):
            self.run_with('aborted')
        self.assertEqual(db.abort_streak(self.conn), (3, ok))
        self.run_with('aborted')
        self.assertEqual(db.abort_streak(self.conn), (4, ok),
                         'the anchor is stable while the stall continues')
        ok2 = self.run_with('ok')
        self.assertEqual(db.abort_streak(self.conn), (0, ok2))
        self.run_with('aborted')
        self.run_with('aborted')
        self.assertEqual(db.abort_streak(self.conn), (2, ok2))

    def test_unfinished_runs_neither_count_nor_reset(self):
        ok = self.run_with('ok')
        self.run_with('aborted')
        self.run_with('aborted')
        rid = db.start_run(self.conn, 'execute', '2026-11-09T09:00:00+00:00')
        self.assertEqual(db.abort_streak(self.conn), (2, ok),
                         'a never-finished row does not count as an abort')


if __name__ == '__main__':
    unittest.main()
