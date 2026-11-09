"""Controller bookkeeping states and the board refresh that follows them.

These states describe what the controller knows and has done. AO has no matching
set of native states, so each one is defined by observable facts (GitHub pull
request state, check rollup, review items) plus AO's ``activity.state`` /
``isTerminated``, and is recomputed from those facts on every tick. The
transitions are not stored as an authoritative flag, which is what makes the
controller crash-safe: a restart derives the same state from the same facts.

    queued                 claim reserved, no worker session yet
    running                worker session alive, no pull request yet
    pr_open                pull request exists, CI not observed yet
    awaiting_ci            CI still running / nothing trustworthy to act on
    fixing_ci              a CI fix request was delivered for this head commit
    awaiting_review        CI green, no open human feedback
    addressing_review      a review fix request was delivered
    ready_for_human_merge  CI green, approved, nothing pending -- a human merges
    merged                 pull request merged; claim retired
    failed                 needs a human: retries exhausted, dead session, closed PR
    paused                 "agent: paused" label present; controller is hands off

The one action in this module, :func:`refresh_board`, exists because the work
board is regenerated only when some ``scripts/wip`` command happens to run: a
claim the controller moved (spawn, merge-retire, quarantine) is invisible on
board #917 until then. The board's format and content belong to ``scripts/wip``
and are duplicated nowhere here -- the controller only invokes it.
"""
import subprocess

from .logging_util import redact

QUEUED = 'queued'
RUNNING = 'running'
PR_OPEN = 'pr_open'
AWAITING_CI = 'awaiting_ci'
FIXING_CI = 'fixing_ci'
AWAITING_REVIEW = 'awaiting_review'
ADDRESSING_REVIEW = 'addressing_review'
READY_FOR_HUMAN_MERGE = 'ready_for_human_merge'
MERGED = 'merged'
FAILED = 'failed'
PAUSED = 'paused'

ALL = (QUEUED, RUNNING, PR_OPEN, AWAITING_CI, FIXING_CI, AWAITING_REVIEW,
       ADDRESSING_REVIEW, READY_FOR_HUMAN_MERGE, MERGED, FAILED, PAUSED)

#: States in which the controller may still deliver work to a worker.
ACTIONABLE = (PR_OPEN, AWAITING_CI, FIXING_CI, AWAITING_REVIEW, ADDRESSING_REVIEW,
              RUNNING, QUEUED)

#: Terminal for scheduling purposes: the issue keeps its claim but nothing runs.
SETTLED = (MERGED, FAILED, READY_FOR_HUMAN_MERGE, PAUSED)

#: AO activity states that consume execution capacity.
CAPACITY_CONSUMING = 'active working busy running'


def refresh_board(cfg, log):
    """Regenerate the ``scripts/wip`` work board after a claim change.

    Called by the tick after any execute-mode pass that moved a claim state
    (spawn, merge-retire, quarantine), because the board of issue #917 is
    otherwise only refreshed by ``wip claim/wait/done`` and silently
    misrepresents reality in between. The board content is entirely
    ``scripts/wip``'s; this function merely runs ``<wip_script> board`` under
    the configured command timeout.

    Any failure -- missing script, non-zero exit, timeout -- is a WARNING and
    never touches the tick: a stale board is an annoyance, a lost tick is an
    incident. Returns True when wip reported a refreshed board.
    """
    script = str(cfg.wip_script or '')
    if not script:
        log.debug('board_refresh_skipped', detail='wip_script not configured')
        return False
    try:
        proc = subprocess.run([script, 'board'], capture_output=True, text=True,
                              timeout=cfg.command_timeout_seconds)
    except Exception as exc:
        log.warning('board_refresh_failed', detail=redact(exc))
        return False
    if proc.returncode != 0:
        log.warning('board_refresh_failed',
                    detail=redact(f'exit {proc.returncode}: '
                                  f'{(proc.stdout or "").strip()} '
                                  f'{(proc.stderr or "").strip()}'))
        return False
    log.info('board_refreshed', detail=script)
    return True
