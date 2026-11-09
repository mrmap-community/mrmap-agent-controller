"""SQLite state.

The pilot's ``claims`` table is kept exactly as it is: same columns, same
meaning, no ALTER. Everything the controller adds lives in new tables, so the
working gatekeeper keeps running against the same file and a fresh install of
either program produces a usable database.

Schema drift is repaired by :func:`ensure_schema`: after ``CREATE TABLE IF NOT
EXISTS`` every column this layout expects but the file lacks is added with
``ALTER TABLE ... ADD COLUMN``. Nothing is ever dropped, renamed or recreated,
and ``PRAGMA user_version`` stamps which layout a file carries. Without this,
a pilot-era ``dispatches`` table kept failing every delivery with
``no such column: attempts`` -- 456 aborted ticks of it (incident 2026-11-06,
root cause B-3.1).

``claims`` is the exclusive assignment board -- ``issue`` is the primary key, so
two inserts for one issue cannot both succeed. ``dispatches.dedupe_key`` is the
second unique index and is what makes CI and review delivery idempotent.
"""
import sqlite3

# Byte-compatible with the pilot gatekeeper's DDL.
CLAIMS_DDL = '''CREATE TABLE IF NOT EXISTS claims (
    issue INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    note TEXT
)'''

# The five columns the pilot created and the only ones this controller may write.
CLAIM_COLUMNS = ('issue', 'state', 'session_id', 'created_at', 'note')

NEW_DDL = '''
CREATE TABLE IF NOT EXISTS issues (
    issue INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    session_id TEXT,
    pr_number INTEGER,
    head_sha TEXT,
    ci_status TEXT,
    review_state TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    issue INTEGER NOT NULL,
    pr_number INTEGER,
    dedupe_key TEXT NOT NULL UNIQUE,
    head_sha TEXT,
    session_id TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS processed_events (
    kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    issue INTEGER,
    pr_number INTEGER,
    batch_key TEXT,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (kind, source_id)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    outcome TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_dispatch_pr ON dispatches(pr_number, kind);
CREATE INDEX IF NOT EXISTS ix_processed_pr ON processed_events(pr_number);
'''

CLAIM_QUEUED = 'queued'
CLAIM_SPAWNING = 'spawning'
CLAIM_SPAWNED = 'spawned'
CLAIM_ADOPTED = 'adopted'
CLAIM_RECONCILE = 'needs_reconciliation'
CLAIM_MERGED = 'merged'
CLAIM_RELEASED = 'released'
# Claim states that still hold the issue (the pilot skips any existing row).
CLAIM_HELD = CLAIM_SPAWNING, CLAIM_SPAWNED, CLAIM_ADOPTED, CLAIM_QUEUED, CLAIM_RECONCILE, CLAIM_MERGED


def connect(path, durable=True):
    """Open the state database.

    ``durable=False`` is for tests only: it skips the fsync that makes a commit
    survive a power loss. Production ticks keep the default durability.
    """
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000')
    if not durable:
        db.execute('PRAGMA synchronous=OFF')
        db.execute('PRAGMA journal_mode=MEMORY')
    return db


#: Layout stamp written to ``PRAGMA user_version``. A file found at 0 is
#: either pilot-era or first-controller-era; PRAGMA, not the stamp, decides
#: which ALTERs are actually needed, so a file stamped 0 that already has a
#: column is repaired without touching that table.
#:   0: pilot-era / unstamped (no controller guarantee)
#:   1: controller tables as of the first release (dispatches has ``attempts``)
#:   2: dispatches gained ``next_retry_at`` (controller-side retry backoff)
SCHEMA_VERSION = 2

#: Columns introduced after the first controller layout, grouped by the
#: version that introduced them. This is the ledger :func:`ensure_schema`
#: consults; the PRAGMA check decides per file which of them are missing.
ADDITIVE_COLUMNS = {
    1: {'dispatches': {'attempts': 'INTEGER NOT NULL DEFAULT 1'}},
    2: {'dispatches': {'next_retry_at': 'TEXT'}},
}


class SchemaVersionError(Exception):
    """The state database carries a newer layout than this code knows."""


def init(db):
    db.execute(CLAIMS_DDL)
    db.executescript(NEW_DDL)
    ensure_schema(db)
    db.commit()


def columns_of(db, table):
    """Column names of one existing table, in file order ([] when absent)."""
    return [r['name'] for r in db.execute(f'PRAGMA table_info({table})')]


def ensure_schema(db):
    """Bring an existing database up to the current layout, additively only.

    Generalises :func:`schema_is_legacy_compatible` from a claims-only promise
    to a checked schema: for every table that exists, each column this layout
    expects but the file lacks gets an ``ALTER TABLE ... ADD COLUMN`` -- never
    a drop, never a recreate. Additive columns are invisible to the legacy
    pilot and to older controller code (inserts name their columns and every
    added column carries a default), so the repair works while the old loop
    still runs against the file.

    ``claims`` takes part in the check but never in the repair: its five
    pilot columns are a contract, enforced by refusing to run
    (``schema_is_legacy_compatible``), not by ALTERing.

    A file stamped with a *newer* layout than this code knows is refused:
    half-understanding a schema written by a later controller is how drift
    becomes silent corruption.

    Returns the list of ``table.column`` additions actually made.
    """
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f'state database is stamped schema version {version}, this '
            f'controller knows at most {SCHEMA_VERSION}; refusing to run '
            f'against a newer layout')
    added = []
    if version >= SCHEMA_VERSION:
        return added
    for _target, tables in sorted(ADDITIVE_COLUMNS.items()):
        for table, columns in sorted(tables.items()):
            if not columns_of(db, table):
                continue                    # table absent: NEW_DDL created it whole
            existing = columns_of(db, table)
            for name, definition in sorted(columns.items()):
                if name in existing:
                    continue
                try:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
                except sqlite3.OperationalError as exc:
                    # A concurrent init (one-shot command beside the loop) may
                    # have added it a moment ago; the PRAGMA settles it.
                    if name not in columns_of(db, table):
                        raise exc
                added.append(f'{table}.{name}')
    db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    db.commit()
    return added


def schema_is_legacy_compatible(db):
    cols = [r['name'] for r in db.execute('PRAGMA table_info(claims)')]
    return cols == ['issue', 'state', 'session_id', 'created_at', 'note']


# ---------------------------------------------------------------- claims ----

def get_claim(db, issue):
    return db.execute('SELECT * FROM claims WHERE issue=?', (issue,)).fetchone()


def all_claims(db):
    return db.execute('SELECT * FROM claims ORDER BY issue').fetchall()


def claim_issue(db, issue, state, now, note, session_id=None):
    """Reserve an issue. Returns True when this caller got the exclusive claim."""
    try:
        db.execute('INSERT INTO claims(%s) VALUES(?,?,?,?,?)'
                   % ','.join(CLAIM_COLUMNS),
                   (issue, state, session_id, now, note))
        db.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def set_claim(db, issue, state, now, note=None, session_id=None):
    if session_id is not None:
        db.execute('UPDATE claims SET state=?, session_id=?, note=? WHERE issue=?',
                   (state, session_id, note, issue))
    elif note is not None:
        db.execute('UPDATE claims SET state=?, note=? WHERE issue=?', (state, note, issue))
    else:
        db.execute('UPDATE claims SET state=? WHERE issue=?', (state, issue))
    db.commit()


def release_claim(db, issue, now, note):
    """Human-authorized release; the row stays for audit and keeps the pilot skipping."""
    db.execute('UPDATE claims SET state=?, note=? WHERE issue=?',
               (CLAIM_RELEASED, note, issue))
    db.commit()


# --------------------------------------------------------------- issues -----

def upsert_issue(db, issue, state, now, session_id=None, pr_number=None,
                head_sha=None, ci_status=None, review_state=None, reason=None):
    existing = db.execute('SELECT * FROM issues WHERE issue=?', (issue,)).fetchone()
    if existing is None:
        db.execute('INSERT INTO issues(issue,state,session_id,pr_number,head_sha,'
                   'ci_status,review_state,reason,created_at,updated_at) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (issue, state, session_id, pr_number, head_sha, ci_status,
                    review_state, reason, now, now))
    else:
        db.execute('UPDATE issues SET state=?, session_id=?, pr_number=?, head_sha=?,'
                   'ci_status=?, review_state=?, reason=?, updated_at=? WHERE issue=?',
                   (state, session_id, pr_number, head_sha, ci_status, review_state,
                    reason, now, issue))
    db.commit()
    return existing


def get_issue(db, issue):
    return db.execute('SELECT * FROM issues WHERE issue=?', (issue,)).fetchone()


def all_issues(db):
    return db.execute('SELECT * FROM issues ORDER BY issue').fetchall()


# ---------------------------------------------------------- dispatches -----

def try_reserve_dispatch(db, kind, issue, dedupe_key, now, pr_number=None,
                        head_sha=None, detail=None):
    """Reserve a delivery slot. Returns the row id, or None when already seen.

    The row is inserted before the message is sent so a crash between "decided"
    and "sent" leaves a visible, non-repeatable record instead of a duplicate
    resume on the next tick.
    """
    try:
        cur = db.execute('INSERT INTO dispatches(kind,issue,pr_number,dedupe_key,'
                         'head_sha,status,created_at,detail) '
                         "VALUES(?,?,?,?,?, 'pending', ?, ?)",
                         (kind, issue, pr_number, dedupe_key, head_sha, now, detail))
        db.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def bump_dispatch(db, dedupe_key):
    """Count another attempt on an existing delivery (a retry, never a new one)."""
    db.execute('UPDATE dispatches SET attempts = attempts + 1, status=? WHERE dedupe_key=?',
               ('pending', dedupe_key))
    db.commit()


def attempt_count(db, pr_number, kinds):
    """How many deliveries this pull request has cost for the given kinds.

    A combined CI-plus-review request counts towards both budgets, because it
    resumes the worker once for both problems.
    """
    marks = ','.join('?' for _ in kinds)
    row = db.execute(
        f'SELECT COALESCE(SUM(attempts), 0) FROM dispatches WHERE pr_number=? '
        f'AND kind IN ({marks})', (pr_number, *kinds)).fetchone()
    return row[0]


def set_dispatch(db, dedupe_key, status, session_id=None, detail=None,
                 next_retry_at=None):
    """Set a delivery's outcome. ``next_retry_at`` replaces the previous
    backoff timestamp outright -- passing None clears it, so a delivered or
    uncertain dispatch never carries a stale retry gate."""
    db.execute('UPDATE dispatches SET status=?, session_id=COALESCE(?, session_id), '
               'detail=COALESCE(?, detail), next_retry_at=? WHERE dedupe_key=?',
               (status, session_id, detail, next_retry_at, dedupe_key))
    db.commit()


def count_dispatches(db, pr_number, kind, statuses=('dispatched', 'uncertain')):
    marks = ','.join('?' for _ in statuses)
    return db.execute(
        f'SELECT COUNT(*) FROM dispatches WHERE pr_number=? AND kind=? '
        f'AND status IN ({marks})', (pr_number, kind, *statuses)).fetchone()[0]


def dispatch_for(db, dedupe_key):
    return db.execute('SELECT * FROM dispatches WHERE dedupe_key=?',
                      (dedupe_key,)).fetchone()


def last_dispatch(db, pr_number, kind):
    return db.execute('SELECT * FROM dispatches WHERE pr_number=? AND kind=? '
                      'ORDER BY id DESC LIMIT 1', (pr_number, kind)).fetchone()


def dispatch_history(db, limit=20):
    return db.execute('SELECT * FROM dispatches ORDER BY id DESC LIMIT ?',
                      (limit,)).fetchall()


# ------------------------------------------------------ processed events ----

def mark_events(db, rows, now):
    """rows: iterable of (kind, source_id, issue, pr_number, batch_key)."""
    db.executemany('INSERT OR IGNORE INTO processed_events(kind,source_id,issue,'
                   'pr_number,batch_key,processed_at) VALUES(?,?,?,?,?,?)',
                   [(k, s, i, p, b, now) for k, s, i, p, b in rows])
    db.commit()


def seen(db, kind, source_id):
    return db.execute('SELECT 1 FROM processed_events WHERE kind=? AND source_id=?',
                      (kind, source_id)).fetchone() is not None


def seen_ids(db, kind, pr_number):
    return {r['source_id'] for r in db.execute(
        'SELECT source_id FROM processed_events WHERE kind=? AND pr_number=?',
        (kind, pr_number))}


# ------------------------------------------------------------------ runs ----

def start_run(db, mode, now):
    cur = db.execute('INSERT INTO runs(started_at,mode) VALUES(?,?)', (now, mode))
    db.commit()
    return cur.lastrowid


def finish_run(db, run_id, now, outcome, detail=None):
    db.execute('UPDATE runs SET finished_at=?, outcome=?, detail=? WHERE id=?',
               (now, outcome, detail, run_id))
    db.commit()


def last_run(db):
    return db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 1').fetchone()


def recent_runs(db, limit=10):
    return db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()


def abort_streak(db):
    """(count, anchor) of the current run of aborted ticks.

    ``count`` is the runs finished with outcome 'aborted' after the newest
    run that finished with any other outcome; ``anchor`` is that newer run's
    id (0 when the ledger holds no such row). The anchor stays stable while a
    stall continues, which is what makes the watchdog's dedupe key one alert
    per episode instead of one per arithmetic window.
    """
    row = db.execute(
        '''SELECT COUNT(*) AS n,
                  (SELECT COALESCE(MAX(id), 0) FROM runs
                     WHERE outcome IS NOT NULL AND outcome != 'aborted') AS anchor
           FROM runs
           WHERE outcome = 'aborted'
             AND id > (SELECT COALESCE(MAX(id), 0) FROM runs
                        WHERE outcome IS NOT NULL AND outcome != 'aborted')'''
    ).fetchone()
    return row['n'], row['anchor']
