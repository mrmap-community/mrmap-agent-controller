# MrMap Agent Controller

Autonomous issue-to-merged-PR control for the mrmap agent fleet. It is the second stage of
the pilot gatekeeper: the pilot proved the approval check, the AO spawn and the SQLite claim
model on one issue (#1102, PR #1103). This controller adds issue intake, the worker limit,
CI-failure recovery and human-review response, and runs on a five-minute timer.

It never approves, merges, reviews, or pushes. Those live with humans and with the worker.

## How a tick works

`mrmap-controller tick` takes the exclusive lock (the same lock file the pilot uses, so the
two schedulers cannot overlap) and runs three phases in one pass:

1. **Reconcile.** Every claim in `state.sqlite3` is matched against AO and GitHub. A claim
   whose AO session has vanished stops being actionable and reports
   `needs_reconciliation` — the controller will not guess. A claim whose issue lost
   `agent: approved` and whose session is already gone is dropped, so intake can take the
   issue again.
2. **Intake.** Issues with every `intake_labels` label, no `block_labels`, no pull request,
   and no session are queued in creation order. Free slots are filled oldest-first. AO
   sessions that exist for an issue are adopted before anything is spawned, so a crash can
   never double-start a worker for the same issue.
3. **PR follow-up.** For each live claim with an open pull request the controller reads CI,
   review threads and issue comments. A red CI, a change request, or a finding from a
   *trusted* account is delivered to the worker's session as a short act item; the session is
   woken or resumed only when that is needed and only if a slot is free.

Capacity is the number of sessions that are actually working, not the number of claims. A
claim whose session is idle is not consuming a worker, but it still holds its issue: the slot
is only released when the pull request is merged or the claim is otherwise settled. That is
deliberate — waking an idle session costs a slot, and AO cannot restore a terminated one.

## Who may ask for work

`approvers` in `config.json` (your account) or a repository OWNER/MEMBER/COLLABORATOR,
never a bot, never a `[bot]` login. The rule is applied per kind of feedback:

- an approval is *always* a human act. A `github.com/app` actor that applied
  `agent: approved` is a workflow marking its own work as approved, so it is rejected;
- CI and review feedback come from the actor GitHub reports on the check run or the review.

Untrusted findings are logged as `ignored_untrusted` and left for a human. The controller
never resolves a review thread and never pushes.

## Commands

```bash
mrmap-controller tick --dry-run     # one pass, writes nothing (default per config)
mrmap-controller tick --execute     # one real pass
mrmap-controller status             # claims, slots, next intake, blockers, open question
mrmap-controller logs --tail 40     # JSONL events
mrmap-controller doctor             # preconditions: gh, AO, capacity, lock, shared state
mrmap-controller loop --once        # foreground, one pass, no detach
mrmap-controller start --execute    # detached loop behind the same lock (non-systemd hosts)
mrmap-controller stop               # stop the detached loop
mrmap-controller backup             # consistent snapshot of the state DB
mrmap-controller release 120 --confirm  # drop a stale claim after checking who owns it
mrmap-controller pause "reason"     # report-only until resume; also blocks delivery
mrmap-controller resume
```

`systemd/` ships the timer + oneshot unit pair (five-minute cadence, non-overlapping,
0600 umask). `install.sh --enable` is what turns it on; without that flag it stays disabled.
On a host without a systemd user bus, `mrmap-controller start` runs the same thing as a
detached loop behind the same lock. `OPERATIONS.md` is the operator document: activation,
recovery, limits, and the configuration table.

## Relationship to the pilot

The controller shares `~/.ao/mrmap-gatekeeper/state.sqlite3` — the *same* `claims` table, the
same `intake.lock`. Its own tables (`issues`, `dispatches`, `processed_events`, `runs`) are
additive, so `gatekeeper.py status` keeps working against the same file. Do not run both
schedulers at the same time; the shared lock makes them wait for each other rather than
corrupt each other, but one of them should be off.
