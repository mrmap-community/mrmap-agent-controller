"""Controller bookkeeping states.

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
"""

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
