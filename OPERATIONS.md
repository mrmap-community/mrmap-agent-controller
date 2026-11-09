# MrMap Agent Controller — operations

The controller is the autonomous layer on top of the pilot gatekeeper. It answers three
questions on a timer:

1. **Which issues may a worker take?** An issue must carry the human-applied
   `agent: approved` label and must not carry `agent: paused`. Nobody inside the
   controller can approve; a `github.com/app` actor with that label is a bot marking its
   own work and is rejected.
2. **Is there room for a worker?** A slot is taken while the AO session is active; a session
   that went idle still holds its issue until the pull request is merged.
3. **What does an open pull request need?** CI failures and review feedback from a
   *trusted* account are delivered into the session that owns the pull request, so CI
   turns red for a few minutes and then goes green again on its own.

It is a systemd timer (or `loop`) running `mrmap-controller tick` every five minutes.
Every decision is in a state row and every state change is one JSON line in
`logs/controller.jsonl`.

---

## Files

| Path | What |
| --- | --- |
| `config.json` | the only file an operator edits (`0600`) |
| `state/` / `state.sqlite3` | **the pilot's database**, shared with the gatekeeper; the controller only adds tables |
| `backups/` | one consistent snapshot of that database per install/backup, `0600` |
| `logs/controller.jsonl` | 5 MB × 5 rotated, secrets redacted |
| `run/paused`, `run/controller.pid` | pause marker, loop pid |
| `bin/mrmap-controller` | entry point |

`state.sqlite3` is the pilot's. The controller's schema is strictly additive: `claims` is
read and written with the pilot's own column names, and `gatekeeper.py status` still works
against a database the controller has touched. The two schedulers share the pilot's
`state/intake.lock`, so a gatekeeper tick and a controller tick can never run at the same
time. **Run at most one scheduler.**

---

## Install, configure, watch

```bash
cd ~/.ao/mrmap-controller
./install.sh                     # safe: checks, backups, installs units, does NOT enable
$EDITOR config.json
mrmap-controller doctor          # 13 checks: gh, AO, config, state, capacity, Docker lock
mrmap-controller tick            # one dry run, writes nothing that matters
mrmap-controller status          # what it would do right now
mrmap-controller logs --tail 40  # last events
```

Decide from the dry run:

```console
$ mrmap-controller tick
dry run · 1 action(s) · 6.5s
  issue #120   → would spawn          … (1/1 slots used, trusted=…)
```

A dry run may say *would deliver* for CI or review events. It never spawns, never resumes,
and never writes a claim. What it does write is one row in its own `runs` table, so the
`runs` count in `status` grows with every report-only tick — the pilot's `claims` table is
byte-identical after a dry run, which `tests/test_intake.py` asserts.

---

## Activation

The scheduler stays disabled until you enable it. Nothing above turns it on.

### Host with systemd (the target deployment)

```bash
./install.sh --enable            # systemctl --user enable --now mrmap-controller.timer
systemctl --user status mrmap-controller.timer
journalctl --user -u mrmap-controller.service -f
```

Or by hand, in case you want to watch the first cycles:

```bash
systemctl --user start mrmap-controller.timer
systemctl --user start mrmap-controller.service     # run one tick immediately
```

### This host (no systemd user bus)

`install.sh --enable` refuses here and tells you to use the loop, which is the same
behaviour without systemd: `setsid`, a pidfile, and the same `flock`, so two loops and a
loop plus a timer can never overlap.

```bash
mrmap-controller start           # detached loop, 300s between ticks
mrmap-controller start --execute # the real thing — only after a good dry run
mrmap-controller loop --once     # foreground, exactly one tick (debugging)
mrmap-controller stop            # TERM, then KILL after 40s; releases the lock
```

`start --execute` is the activation command on this host. Before running it, flip
`"dry_run": false` in `config.json` if you want the loop to act without `--execute`.

### To turn it off

```bash
mrmap-controller pause "reason"  # report-only, keeps ticking and logging
systemctl --user stop mrmap-controller.timer   # or: mrmap-controller stop
```

`pause` is the switch to reach for first: a paused controller reports what it would do,
touches nothing, and — because that also blocks delivery — a paused controller will not
interrupt a worker you are watching. `resume` returns to normal.

---

## What the controller may and may not do

| | |
| --- | --- |
| May | read GitHub, `ao spawn` / `ao session wake` / `ao session resume --send`, write its own state, answer to your GitHub account only for those three `ao` calls |
| Must not | merge, approve, comment, close, resolve, push, edit GitHub labels or permissions, kill a session, delete a volume, run any Docker command |

The prohibition is structural, not a prompt: every external call goes through a runner with
a deny list, so `gh pr merge`, `gh api -X POST`, `ao pr merge`,
`ao session kill` and `docker …` fail inside the controller before they reach a binary. A
worker gets the same list as deny permissions on its `opencode` session and is instructed to
run heavy tests only through `mrmap-docker-test`, which serialises Docker stacks.

**Merging is the human's job.** When CI is green and no trusted finding is unanswered, the
controller moves the issue to `ready_for_human_merge`, releases its claim, and says so. The
worker's `scripts/wip done` has already released the board claim; the controller's row says
how the work ended.

---

## Recovery

### Controller crashed / machine rebooted

Nothing to decide: the timer starts the next tick and the first thing a tick does is
reconcile. A row whose AO session vanished goes to `needs_reconciliation` and is never
auto-resumed; a claim whose issue lost `agent: approved` and whose session is already gone
is dropped; a claim whose session still exists becomes `running`.

### A claim is stuck and you are sure the session is gone

```bash
mrmap-controller status --issue 120     # read what it sees and why
mrmap-controller backup                 # snapshot first
mrmap-controller release 120 --confirm  # deletes the claim row; intake may take the issue again
```

`release` refuses without `--confirm`. It is the only command in the tool that lets a second
worker be assigned to an issue. The row itself is kept, with state `released`, as the audit
trail — the controller reads that as free, the pilot would still read any row as held. Only
one of the two is ever running, so this cannot disagree with itself; if you ever hand the
issue back to the pilot, delete the row.

### You want to start over on an issue

Apply the pause label rather than releasing, then remove it when you mean it:

```bash
gh issue edit 120 --add-label "agent: paused"    # the controller stops touching it
gh issue edit 120 --remove-label "agent: paused" # it picks the issue up again next tick
```

### The state database itself

```bash
mrmap-controller backup                       # → backups/state-<UTC>.sqlite3, 0600
ls ~/.ao/mrmap-controller/backups
```

To restore, stop the scheduler, move the current file aside and copy a backup in place:

```bash
systemctl --user stop mrmap-controller.timer   # or: mrmap-controller stop
cp -a backups/state-20261106T150000Z.sqlite3 state.sqlite3
```

The controller never changes the database's format: it keeps the journal mode the pilot
created (`delete`), writes only the pilot's five `claims` columns, and adds four tables of
its own. Verified against a copy of the live database: the pilot's `init_db` runs on the
migrated file, its `SELECT * FROM claims` reads rows the controller wrote, it can write back,
and its `active_workers` — which counts AO sessions, not claims — ignores the controller's
extra states (`merged`, `needs_reconciliation`, `released`) instead of tripping over them.

### A worker was started but never appeared

`ao spawn` prints the new session on a line; if that read fails the controller records
`start_uncertain` and will not start a second one. Run `mrmap-controller status`: if the
issue reports `start_uncertain` and you can see no session, `release` it and let intake
retry. If you *can* see the session, the next tick adopts it — intake looks up by issue id
before it ever spawns.

---

## Reading the log

```bash
mrmap-controller logs --tail 40
grep -e '"event": "SPAWN' -e '"event": "DELIVERY_FAILED"' logs/controller.jsonl
python3 - <<'PY'
import json
for line in open('/home/ota/.ao/mrmap-controller/logs/controller.jsonl'):
    r = json.loads(line)
    if r.get('event') == 'TICK_SUMMARY':
        print(r['ts'], r.get('report_only'), r.get('seconds'), r.get('actions'))
PY
```

Events worth watching: `NO_APPROVED_LABEL` (intake idle, which is the normal state —
approval is a human act), `capacity_full` / `deferred_capacity` (slots are full),
`DELIVERY_FAILED` (the AO send was refused; the event stays retryable, up to the configured
cap), `claim_needs_reconciliation`, `STALE` (a claim older than `stale_after_hours`).

---

## Configuration

| Key | Default | Notes |
| --- | --- | --- |
| `dry_run` | `true` | the install default; `tick` flips it off for one run |
| `max_workers` | `1` | the rollout default. The feature caps at 3; above 3 is refused |
| `poll_seconds` | `300` | used by the units and the loop |
| `intake_labels` | `["agent: approved"]` | all must be present on the issue |
| `block_labels` | `["agent: paused"]` | any present stops it; takes precedence over approval |
| `approvers` | `["jokiefer"]` | the only account that may trigger a CI fix or a review response |
| `max_ci_attempts_per_pr` / `max_review_rounds_per_pr` | `3` / `3` | per PR, counts **dispatches**, not deliveries: a delivery that never reached the session does not spend a round |
| `min_seconds_between_dispatches` | `120` | grace so a new commit is not read as a fresh failure |
| `stale_after_hours` | `48` | a claim older than this with no session is reported |
| `capacity_command` | `…/scripts/agent_capacity` | may be empty, e.g. on a dedicated host |
| `allow_restore_terminated` | `false` | AO does not restore a terminated session on Linux; this is why the limit holds |
| `ao.project` | `mrmap-8` | which AO project to spawn into |
| `github.exclude_labels` | `[epic, type: documentation]` | never assigned to a worker |

`approvers` is the whole trust model for follow-up work. It starts with one account. A
collaborator whose feedback you want acted on has to be added here **or** hold
OWNER/MEMBER/COLLABORATOR on the repository; a review from anyone else is logged as
`ignored_untrusted` and left for a human.

---

## Limits this build does not remove

- **No auto-merge and no auto-approval.** Not configurable, on purpose.
- **Capacity is conservative.** A claim holds a slot until the PR is merged, so one
  merged-but-unreconciled session can block intake for one tick. The reconciler clears it
  on the next tick.
- **A delivery that returns without an error is not proof.** AO reports
  `connected: false` for the *previous* state when it wakes a session, so the controller
  re-reads the session and only calls a delivery done when AO then reports it connected. If
  that read is inconclusive, the event becomes `uncertain`: not retried (AO may well be
  working on it), and visible in `status` as `delivery_uncertain`.
- **`ao pr watch` stays off.** That feature tracks PRs through a per-PR daemon and has no
  unwatch; the controller polls instead, which is restart-safe.
- AO's own automation ("MrMap GitHub Issue Intake") must stay off while the controller is
  on — `install.sh` refuses to enable both. The controller leaves it alone.
