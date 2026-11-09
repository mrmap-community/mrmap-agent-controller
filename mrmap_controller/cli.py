"""Command line interface.

Every mutating subcommand is explicit. There is no subcommand that approves,
merges, closes or resolves anything -- that surface does not exist here, and the
runner refuses the underlying commands even if a future code path tried.
"""
import argparse
import datetime as dt
import errno
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, ao as ao_mod, config as config_mod, db, github as gh_mod
from .core import Controller, claim_holds, loop_watchdog
from .logging_util import redact, setup
from .runner import Runner, assert_allowed

INSTALL = Path(__file__).resolve().parent.parent
PAUSE_MARKER = INSTALL / 'paused'
RUN_DIR = INSTALL / 'run'
PIDFILE = RUN_DIR / 'controller.pid'


# ------------------------------------------------------------------ helpers --
def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def build(args, verbose=False):
    cfg = config_mod.load(getattr(args, 'config', None))
    log = setup(cfg.log_dir, verbose=verbose or getattr(args, 'verbose', False))
    return cfg, log


def open_db(cfg, log):
    dbfile = Path(cfg.state_db)
    if not dbfile.exists() and dbfile.parent.exists():
        log.info('state_database_created', detail=str(dbfile))
    conn = db.connect(dbfile)
    db.init(conn)
    if not db.schema_is_legacy_compatible(conn):
        raise SystemExit('the claims table does not match the pilot schema; refusing '
                         'to run against this database')
    return conn


def exclusive_lock(cfg, log, block=False):
    """One controller at a time, sharing the pilot's intake lock file."""
    Path(cfg.lock_file).parent.mkdir(parents=True, exist_ok=True)
    handle = open(cfg.lock_file, 'a+')
    try:
        import fcntl
        flags = fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        handle.close()
        log.warning('another_run_in_progress', detail=cfg.lock_file)
        return None
    except OSError as exc:
        handle.close()
        raise SystemExit(f'cannot take the intake lock: {redact(exc)}')
    return handle


def loop_instance_lock(cfg, log):
    """Hard single-instance guard for the scheduler loop (W4).

    The pidfile check in ``cmd_loop`` is advisory: it races and it does not
    notice a loop that never wrote one. The flock on ``<state_dir>/loop.lock``
    is not: the kernel drops it exactly when the holder dies. The handle is
    returned, deliberately not closed -- the caller must keep it open for the
    loop's lifetime. Ticks are unaffected, and a loop already running without
    this guard is never touched; the refusal applies from the next start.
    """
    lock_path = Path(cfg.state_db).parent / 'loop.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+')
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        log.warning('loop_instance_locked', detail=str(lock_path))
        return None
    except OSError as exc:
        handle.close()
        raise SystemExit(f'cannot take the loop lock {lock_path}: {redact(exc)}')
    return handle


def controller_runner(cfg, log):
    return Runner(default_timeout=cfg.command_timeout_seconds)


# ------------------------------------------------------------- subcommands --
def cmd_tick(args):
    cfg, log = build(args)
    lock = exclusive_lock(cfg, log)
    if lock is None:
        print('Another controller or gatekeeper run holds the intake lock; '
              'nothing was done.')
        return 0
    execute = args.execute and not args.dry_run
    conn = open_db(cfg, log)
    try:
        runner = controller_runner(cfg, log)
        controller = Controller(
            cfg, gh_mod.GitHub(runner, cfg.repo),
            ao_mod.AO(runner, cfg.ao_project, timeout=cfg.command_timeout_seconds),
            conn, log, pause_file=PAUSE_MARKER, execute=execute)
        report = controller.tick()
    finally:
        conn.close()
        lock.close()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f'MrMap Agent Controller {__version__} -- {report["mode"].upper()}'
              f' -- {report["capacity"]}')
        for action in report['actions']:
            bits = [f'{k}={action[k]}' for k in ('issue', 'pr', 'session', 'sha')
                    if action.get(k) is not None]
            print(f'  {action["action"]:18s} ' + ' '.join(bits)
                  + (f'  {action["reason"]}' if action.get('reason') else ''))
        print('  counts: ' + json.dumps(report['counts'], sort_keys=True))
        if report['mode'] == 'dry-run':
            print('  dry run: no AO session was started, no message was delivered, '
                  'the assignment board was not modified.')
    return 0


def _scheduler_state():
    """Which scheduler is live right now: loop pid, systemd timer, or none."""
    state = {'loop_pid': None, 'loop_running': False, 'systemd_timer': 'unknown',
             'pause_marker': PAUSE_MARKER.exists()}
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
        except ValueError:
            pid = None
        if pid:
            state['loop_pid'] = pid
            try:
                os.kill(pid, 0)
                state['loop_running'] = True
            except OSError as exc:
                state['loop_running'] = exc.errno == errno.EPERM
    probe = subprocess.run(['systemctl', '--user', 'is-active',
                            'mrmap-controller.timer'],
                           capture_output=True, text=True)
    if probe.returncode in (0, 3):
        state['systemd_timer'] = probe.stdout.strip() or 'inactive'
    else:
        state['systemd_timer'] = 'no systemd user instance on this host'
    return state


def cmd_status(args):
    cfg, log = build(args)
    conn = open_db(cfg, log)
    try:
        claims = {row['issue']: row for row in db.all_claims(conn)}
        issues = {row['issue']: row for row in db.all_issues(conn)}
        runner = controller_runner(cfg, log)
        gh, ao = gh_mod.GitHub(runner, cfg.repo), ao_mod.AO(runner, cfg.ao_project)
        who, ao_ok, automations = None, None, []
        try:
            who = gh.whoami()
        except gh_mod.GithubError as exc:
            log.warning('github_read_failed', reason=redact(exc))
        try:
            automations = ao.automations()
            ao_ok = True
        except ao_mod.AOError as exc:
            log.warning('ao_read_failed', reason=redact(exc))
        payload = {
            'version': __version__,
            'config_source': getattr(cfg, '_source', None),
            'mode': 'execute' if not cfg.dry_run else 'dry-run',
            'max_workers': cfg.max_workers,
            'poll_interval_minutes': cfg.poll_interval_minutes,
            'repo': cfg.repo,
            'ao_project': cfg.ao_project,
            'state_db': cfg.state_db,
            'github_login': who,
            'ao_reachable': ao_ok,
            'ao_automations_enabled': [a.get('id') for a in automations
                                       if a.get('enabled')],
            'scheduler': _scheduler_state(),
            'last_run': dict(db.last_run(conn)) if db.last_run(conn) else None,
            'issues': [
                {'issue': number,
                 'claim': claims[number]['state'] if number in claims else None,
                 'state': issues[number]['state'] if number in issues else 'untracked',
                 'session': (issues[number]['session_id'] if number in issues
                             else None) or (claims[number]['session_id']
                                            if number in claims else None),
                 'pr': issues[number]['pr_number'] if number in issues else None,
                 'ci': issues[number]['ci_status'] if number in issues else None,
                 'reason': issues[number]['reason'] if number in issues else None}
                for number in sorted(set(claims) | set(issues))],
            'deliveries': [dict(r) for r in db.dispatch_history(conn, 15)],
        }
    finally:
        conn.close()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    sch = payload['scheduler']
    print(f'MrMap Agent Controller {__version__}  '
          f'({"EXECUTE" if payload["mode"] == "execute" else "DRY RUN"})')
    print(f'  config: {payload["config_source"]}')
    print(f'  repo: {payload["repo"]}  ao project: {payload["ao_project"]}  '
          f'gh login: {payload["github_login"] or "UNAVAILABLE"}')
    print(f'  worker limit: {payload["max_workers"]}  '
          f'poll: every {payload["poll_interval_minutes"]} min')
    print(f'  scheduler: loop pid={sch["loop_pid"]} running={sch["loop_running"]}; '
          f'systemd timer={sch["systemd_timer"]}; '
          f'pause marker={"yes" if sch["pause_marker"] else "no"}')
    enabled = payload['ao_automations_enabled']
    print(f'  AO automations enabled: {enabled or "none (correct: one scheduler)"}')
    run = payload['last_run']
    if run:
        print(f'  last tick: {run["started_at"]} mode={run["mode"]} '
              f'outcome={run["outcome"]} counts={run["detail"]}')
    else:
        print('  last tick: never')
    print('  tracked issues:')
    if not payload['issues']:
        print('    none')
    for row in payload['issues']:
        print(f'    #{row["issue"]:<6} claim={row["claim"] or "-":<22} '
              f'state={row["state"]:<22} session={row["session"] or "-":<16} '
              f'pr={row["pr"] or "-":<6} ci={row["ci"] or "-"}')
        if row['reason']:
            print(f'           reason: {redact(row["reason"])}')
    print('  recent deliveries:')
    if not payload['deliveries']:
        print('    none')
    for row in payload['deliveries'][:10]:
        print(f'    {row["created_at"]} {row["kind"]:<9} issue={row["issue"]} '
              f'pr={row["pr_number"] or "-"} status={row["status"]:<10} '
              f'key={row["dedupe_key"]}')
    return 0


def cmd_logs(args):
    cfg, _ = build(args)
    path = Path(cfg.log_dir) / 'controller.jsonl'
    if not path.exists():
        print(f'no log file yet: {path}')
        return 0
    lines = path.read_text().splitlines()[-args.lines:]
    for line in lines:
        if args.level:
            try:
                if json.loads(line).get('level') != args.level.upper():
                    continue
            except ValueError:
                pass
        print(line)
    return 0


def cmd_doctor(args):
    cfg, log = build(args)
    runner = controller_runner(cfg, log)
    checks = []

    def check(name, ok, detail=''):
        checks.append({'name': name, 'ok': bool(ok), 'detail': redact(str(detail))})
        print(f'  [{"ok  " if ok else "FAIL"}] {name}: {redact(str(detail))}')

    print(f'MrMap Agent Controller {__version__} doctor')
    check('python', sys.version_info >= (3, 9), sys.version.split()[0])
    check('config', True, getattr(cfg, '_source', 'defaults'))
    if cfg.max_workers > 3:
        check('worker limit', False, f'{cfg.max_workers} exceeds the host cap of 3')
    else:
        check('worker limit', True, f'{cfg.max_workers} (dry_run={cfg.dry_run})')
    check('state database', Path(cfg.state_db).parent.exists(), cfg.state_db)
    try:
        conn = open_db(cfg, log)
        claims = db.all_claims(conn)
        check('claims table compatible with pilot', db.schema_is_legacy_compatible(conn),
              f'{len(claims)} existing claim row(s) preserved')
        conn.close()
    except SystemExit as exc:
        check('claims table compatible with pilot', False, exc)
    check('intake lock', Path(cfg.lock_file).parent.exists(), cfg.lock_file)
    wrapper = Path(cfg.docker_test_wrapper)
    check('docker test wrapper', wrapper.is_file() and os.access(wrapper, os.X_OK),
          cfg.docker_test_wrapper)
    gate = Path(cfg.capacity_script)
    check('host capacity gate', gate.is_file(), cfg.capacity_script)
    from .lifecycle import board_repository
    wip = Path(cfg.wip_script)
    root = Path(board_repository(cfg.wip_script) or '')
    check('work board tool', bool(str(cfg.wip_script)) and wip.is_file()
          and os.access(wip, os.X_OK) and (root / '.git').exists(),
          f'{cfg.wip_script or "not configured"}'
          + (f' runs inside {root}' if str(cfg.wip_script) else ''))
    gh = gh_mod.GitHub(runner, cfg.repo)
    try:
        check('github authentication (gh)', True, f'logged in as {gh.whoami()}')
    except gh_mod.GithubError as exc:
        check('github authentication (gh)', False, exc)
    ao = ao_mod.AO(runner, cfg.ao_project)
    try:
        sessions = ao.sessions()
        live = [s.id for s in sessions if s.is_worker and not s.is_terminated]
        check('ao daemon', True, f'{len(live)} live worker session(s): {live or "none"}')
    except ao_mod.AOError as exc:
        check('ao daemon', False, exc)
    try:
        enabled = [a for a in ao.automations() if a.get('enabled')]
        check('single scheduler (AO automations disabled)', not enabled,
              f'enabled: {[a.get("displayName") for a in enabled] or "none"}')
    except ao_mod.AOError as exc:
        check('single scheduler (AO automations disabled)', False, exc)
    state = _scheduler_state()
    check('this controller scheduler', True,
          f'loop running={state["loop_running"]} (pid {state["loop_pid"]}), '
          f'systemd timer={state["systemd_timer"]}, '
          f'pause marker={"present" if state["pause_marker"] else "absent"}')
    try:
        assert_allowed(['gh', 'api', 'user'])
        denied = False
        try:
            assert_allowed(['gh', 'pr', 'merge', '1'])
            denied = False
        except Exception:
            denied = True
        check('deny list active (merge/approve impossible)', denied)
    except Exception as exc:
        check('deny list active (merge/approve impossible)', False, exc)
    worst = sum(1 for c in checks if not c['ok'])
    print(f'doctor: {len(checks) - worst}/{len(checks)} checks passed')
    if args.json:
        print(json.dumps(checks, indent=2, sort_keys=True))
    return 1 if worst else 0


def cmd_backup(args):
    cfg, log = build(args)
    source = Path(cfg.state_db)
    if not source.exists():
        print(f'nothing to back up: {source} does not exist')
        return 1
    dest_dir = Path(args.dest or cfg.backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    dest = dest_dir / f'state-{stamp}.sqlite3'
    # sqlite3's "with" commits but never closes, and a backup runs inside the
    # loop's process; a connection left open here is a descriptor left open.
    src = db.connect(source)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
        rows = dst.execute('SELECT COUNT(*) FROM claims').fetchone()[0]
    finally:
        dst.commit()
        dst.close()
        src.close()
    mode = dest.stat().st_mode & 0o777
    dest.chmod(0o600)
    log.info('state_backed_up', detail=f'{dest} claims={rows}')
    print(f'backed up {source} -> {dest} ({rows} claim row(s), mode 0600)')
    return 0


def cmd_release(args):
    cfg, log = build(args)
    if not args.confirm:
        print('Refusing: releasing a claim lets a worker be started for this issue '
              'again. Re-run with --confirm after checking who owns the work.')
        return 2
    lock = exclusive_lock(cfg, log)
    if lock is None:
        print('The intake lock is held by a running tick; try again when it ends.')
        return 1
    conn = open_db(cfg, log)
    try:
        claim = db.get_claim(conn, args.issue)
        if not claim:
            print(f'No claim row for issue #{args.issue}.')
            return 1
        db.release_claim(conn, args.issue, now_iso(),
                         args.note or f'released by operator: {args.who or "unnamed"}')
        log.warning('claim_released', issue=args.issue, detail=args.note or '')
        print(f'Released claim for #{args.issue} (was state={claim["state"]}, '
              f'session={claim["session_id"]}). Intake may assign it again.')
        return 0
    finally:
        conn.close()
        lock.close()


def cmd_reconcile_closing(args):
    """W2: list merged-PR-with-still-open-issue pairs as a human action list.

    The #981/#995 pattern in command form: the DB says merged, GitHub says the
    issue is still open, and only a human closes it. This subcommand reads the
    claims it retired as merged, asks GitHub whether each issue is still open,
    and prints what a human should look at. It prints only: it mutates no
    state, posts no comment and closes nothing.
    """
    cfg, log = build(args)
    conn = open_db(cfg, log)
    try:
        gh = gh_mod.GitHub(controller_runner(cfg, log), cfg.repo)
        pairs = []
        for claim in db.all_claims(conn):
            if claim['state'] != db.CLAIM_MERGED:
                continue
            row = db.get_issue(conn, claim['issue'])
            pr_number = row['pr_number'] if row else None
            if not pr_number:
                pairs.append((claim['issue'], None, 'no PR recorded on the claim',
                              None))
                continue
            try:
                state = (gh.issue(claim['issue']) or {}).get('state')
            except gh_mod.GithubError as exc:
                pairs.append((claim['issue'], pr_number,
                              f'issue state unreadable: {redact(exc)}', None))
                continue
            if state == 'open':
                pairs.append((claim['issue'], pr_number,
                              'still open after merge: close it if the PR '
                              'delivered the work', state))
        print(f'mrmap-controller reconcile-closing ({cfg.repo}, '
              f'{getattr(cfg, "_source", "defaults")})')
        print(f'Merged claims needing a human look ({len(pairs)} item(s)):')
        for issue, pr_number, note, state in sorted(pairs):
            print(f'  #{issue:<6} PR #{pr_number or "?":<6} {note}')
        if not pairs:
            print('  none')
        print('Close issues yourself where the merged PR delivered the work; '
              'this command never closes, comments or mutates anything.')
        return 0
    finally:
        conn.close()


def cmd_pause(args):
    RUN_DIR.mkdir(exist_ok=True)
    PAUSE_MARKER.write_text(args.reason or 'paused by operator\n')
    print(f'Controller paused: {PAUSE_MARKER}. Ticks stay report-only until '
          f'"mrmap-controller resume".')
    return 0


def cmd_resume(args):
    if PAUSE_MARKER.exists():
        PAUSE_MARKER.unlink()
    print('Controller resumed: ticks act again (subject to --dry-run and capacity).')
    return 0


def cmd_loop(args):
    """Foreground supervised loop for hosts without a service manager."""
    cfg, log = build(args)
    interval = int((args.interval or cfg.poll_interval_minutes) * 60)
    stop = {'requested': False}

    def handler(signum, _frame):
        stop['requested'] = True
        log.info('loop_stop_requested', detail=f'signal {signum}')

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    # W4: refuse a second loop from this start onwards. The advisory pidfile
    # check below stays as the friendly message for the common case; the flock
    # is the enforcing guard and the handle lives as long as this function.
    loop_lock = loop_instance_lock(cfg, log)
    if loop_lock is None:
        print('Another loop holds <state_dir>/loop.lock; refusing to start a '
              'second scheduler. The running loop was not touched.')
        return 1
    try:
        RUN_DIR.mkdir(exist_ok=True)
        if args.write_pidfile:
            if PIDFILE.exists():
                existing = PIDFILE.read_text().strip()
                try:
                    os.kill(int(existing), 0)
                    print(f'loop already running with pid {existing}; refusing')
                    return 1
                except (ValueError, OSError):
                    pass
            PIDFILE.write_text(str(os.getpid()))
        log.info('loop_start', detail=f'interval={interval}s mode='
                                      f'{"execute" if not args.dry_run else "dry-run"}')
        execute = not args.dry_run
        while not stop['requested']:
            started = time.time()
            _run_once(cfg, log, execute)
            elapsed = time.time() - started
            if stop['requested']:
                break
            deadline = started + interval
            while not stop['requested'] and time.time() < deadline:
                time.sleep(min(5, max(0.1, deadline - time.time())))
    finally:
        if args.write_pidfile and PIDFILE.exists():
            try:
                if int(PIDFILE.read_text().strip()) == os.getpid():
                    PIDFILE.unlink()
            except (ValueError, OSError):
                pass
        loop_lock.close()
        log.info('loop_end')
    return 0


def _run_once(cfg, log, execute):
    lock = exclusive_lock(cfg, log)
    if lock is None:
        log.info('tick_skipped_locked')
        return
    conn = open_db(cfg, log)
    try:
        runner = controller_runner(cfg, log)
        gh = gh_mod.GitHub(runner, cfg.repo)
        controller = Controller(
            cfg, gh, ao_mod.AO(runner, cfg.ao_project, timeout=cfg.command_timeout_seconds),
            conn, log, pause_file=PAUSE_MARKER, execute=execute)
        try:
            controller.tick()
        except Exception as exc:
            log.error('tick_crashed', reason=redact(exc), detail='loop continues')
        # W4 watchdog: after this tick's runs row is final, look for a streak
        # of aborted ticks and alert the operator once per stall episode. A
        # broken watchdog must never end the loop either.
        try:
            loop_watchdog(conn, gh, log, cfg.board_issue)
        except Exception as exc:
            log.error('watchdog_error', reason=redact(exc))
    finally:
        conn.close()
        lock.close()


def cmd_start(args):
    cfg, log = build(args)
    state = _scheduler_state()
    if state['loop_running']:
        print(f'loop already running (pid {state["loop_pid"]}); use "restart"')
        return 1
    RUN_DIR.mkdir(exist_ok=True)
    logfile = Path(cfg.log_dir) / 'loop.log'
    command = [sys.executable, '-m', 'mrmap_controller.cli', 'loop',
               '--write-pidfile']
    if args.dry_run or cfg.dry_run:
        command.append('--dry-run')
    env = dict(os.environ)
    if getattr(cfg, '_source', None):
        env['MRMAP_CONTROLLER_CONFIG'] = str(cfg._source)
    with open(logfile, 'ab') as out:
        proc = subprocess.Popen(['setsid'] + command, stdout=out, stderr=out,
                                cwd=str(INSTALL), env=env, start_new_session=True)
    print(f'loop started (pid {proc.pid}), interval '
          f'{cfg.poll_interval_minutes} min, output: {logfile}')
    print('It holds the same intake lock as a tick, so ticks never overlap.')
    return 0


def cmd_stop(args):
    state = _scheduler_state()
    pid = state['loop_pid']
    if not pid or not state['loop_running']:
        print('loop is not running')
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(60):
        try:
            os.kill(pid, 0)
        except OSError:
            print(f'loop (pid {pid}) stopped')
            return 0
        time.sleep(0.5)
    print(f'loop (pid {pid}) still alive after SIGTERM; it is finishing a tick')
    return 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog='mrmap-controller',
        description='MrMap Agent Controller: approved GitHub issues to AO workers, '
                    'then CI and human review feedback back to those workers.')
    parser.add_argument('--config', help='path to config.json')
    parser.add_argument('-v', '--verbose', action='store_true')
    sub = parser.add_subparsers(dest='command', required=True)

    tick = sub.add_parser('tick', help='run one intake + PR pass (default: dry run)')
    tick.add_argument('--execute', action='store_true',
                      help='allow starting workers and delivering messages')
    tick.add_argument('--dry-run', action='store_true',
                      help='force report-only even if config says otherwise')
    tick.add_argument('--json', action='store_true')
    tick.set_defaults(func=cmd_tick)

    status = sub.add_parser('status', help='what the controller currently believes')
    status.add_argument('--json', action='store_true')
    status.set_defaults(func=cmd_status)

    logs = sub.add_parser('logs', help='print structured log lines')
    logs.add_argument('-n', '--lines', type=int, default=50)
    logs.add_argument('--level', help='filter, e.g. WARNING or ERROR')
    logs.set_defaults(func=cmd_logs)

    doctor = sub.add_parser('doctor', help='check every precondition')
    doctor.add_argument('--json', action='store_true')
    doctor.set_defaults(func=cmd_doctor)

    backup = sub.add_parser('backup', help='consistent backup of the state database')
    backup.add_argument('--dest', help='directory (default: config backup_dir)')
    backup.set_defaults(func=cmd_backup)

    release = sub.add_parser('release',
                             help='human-authorized release of a quarantined claim')
    release.add_argument('issue', type=int)
    release.add_argument('--confirm', action='store_true')
    release.add_argument('--note')
    release.add_argument('--who', default=os.environ.get('USER', 'unknown'),
                         help='name recorded in the audit note')
    release.set_defaults(func=cmd_release)

    closing = sub.add_parser(
        'reconcile-closing',
        help='list merged PRs whose issue is still open (prints only, W2)')
    closing.set_defaults(func=cmd_reconcile_closing)

    pause = sub.add_parser('pause', help='make every tick report-only')
    pause.add_argument('reason', nargs='?')
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser('resume', help='undo "pause"')
    resume.set_defaults(func=cmd_resume)

    loop = sub.add_parser('loop', help='run ticks in a foreground loop (no systemd)')
    loop.add_argument('--interval', type=float, help='minutes between ticks')
    loop.add_argument('--dry-run', action='store_true')
    loop.add_argument('--write-pidfile', action='store_true')
    loop.set_defaults(func=cmd_loop)

    start = sub.add_parser('start', help='start the detached loop')
    start.add_argument('--dry-run', action='store_true')
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser('stop', help='stop the detached loop')
    stop.set_defaults(func=cmd_stop)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print('interrupted', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
