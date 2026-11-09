# MrMap Agent Controller — operations

The controller is the autonomous layer on top of the pilot gatekeeper. It answers
five questions on a timer:

1. **Which issues may a worker take?** An issue must carry the human-applied
   `agent: approved` label and must not carry `agent: paused`. Nobody inside the
   controller can approve; a `github.com/app` actor with that label is a bot
   marking its own work and is rejected.
2. **Is there room for a worker?** A slot is taken while the AO session is
   *executing*; an idle or exited-but-alive session holds its issue but consumes
   no slot.
3. **What does an open pull request need?** CI failures and review feedback
   from *trusted* accounts are delivered into the session that owns the pull
   request.
4. **Did a merged pull request actually close its issue?** GitHub's real closing
   linkage is checked on merge; when it did not fire, one comment asks a human
   to close (W2, see below). The controller closes nothing.
5. **Has a human answered an open question?** Answered `scripts/wip ask`
   questions and `scripts/decisions` decision requests wake their waiting
   workers (W3, see below), gated by `questions_enabled`.

Every decision is one row in the state database and one JSON line in
`logs/controller.jsonl`. The tick's `runs` ledger row keeps the outcome and the
action counts — that ledger is what proved the November incident, keep it.

---

## Files

| Path | What |
| --- | --- |
| `config.json` | the only file an operator edits (`0600`) |
| `config.example.json` | every real key with its shipped default |
| `state.sqlite3` (at `state_db`) | **the pilot's database**, shared with the gatekeeper; the controller only adds tables and columns, additively |
| `<state_dir>/loop.lock` | flock taken by every `loop` — a second loop refuses to start (W4) |
| `state/intake.lock` | the pilot's intake lock; shared, so a gatekeeper tick and a controller tick never overlap |
| `backups/` | consistent `sqlite3 .backup` snapshots (`0600`) plus the W0 `state-before-W1.sqlite3` and the `pre-fixes-2026-11-09/` tar snapshot |
| `logs/controller.jsonl` | structured events, rotated; the loop's own stdout/stderr go to `logs/loop.log` |
| `paused` | pause marker (created by `pause`, removed by `resume`) |
| `run/controller.pid` | pidfile of the detached loop (advisory; the flock enforces) |
| `bin/mrmap-controller` | entry point: exports `MRMAP_CONTROLLER_CONFIG` and `PYTHONPATH`, execs `python3 -m mrmap_controller.cli` |
| `systemd/` | unit files for hosts that have them — **inert on this host** (no systemd user bus) |

The schema is strictly additive and stamped with `PRAGMA user_version`
(`ensure_schema` in `db.py`): columns this layout expects but the file lacks are
added with `ALTER TABLE ... ADD COLUMN`, never dropped or recreated, so the
legacy pilot keeps running against the same file. A database stamped *newer*
than the code knows is refused rather than half-understood.

---

## Configuration

Config is one JSON file (`$MRMAP_CONTROLLER_CONFIG`, default
`<install>/config.json`). Unknown keys in the file are a hard error — the doc
and the code cannot drift apart silently. Any key can also be overridden with
the environment variable `MRMAP_CONTROLLER_<KEY_UPPER>`. These are **all** the
keys that exist (from `mrmap_controller/config.py`):

| Key | Default | Notes |
| --- | --- | --- |
| `repo` | `mrmap-community/mrmap` | owner/name |
| `ao_project` | `mrmap` | which AO project to spawn into |
| `state_db` | `/home/ota/.ao/mrmap-gatekeeper/state.sqlite3` | the pilot's file |
| `lock_file` | `/home/ota/.ao/mrmap-gatekeeper/intake.lock` | the pilot's exclusive intake lock |
| `log_dir` | `<install>/logs` | |
| `backup_dir` | `<install>/backups` | target of `backup` |
| `dry_run` | `true` | the install default; `tick --execute` overrides per run |
| `max_workers` | `1` | executing AO sessions allowed; capped at 3 by validation |
| `poll_interval_minutes` | `5` | loop interval |
| `approved_label` | `agent: approved` | the only intake trigger |
| `paused_label` | `agent: paused` | present on an issue → the controller is hands off |
| `approvers` | `["jokiefer"]` | accounts whose `agent: approved` label event counts (the latest event must be a labeled event by one of them) |
| `review_allowlist` | `["jokiefer"]` | who may direct a worker through PR feedback |
| `review_associations` | `["OWNER","MEMBER","COLLABORATOR"]` | the author associations trusted on reviews **and on question answers** |
| `ignore_authors` | `["mrmap-agent-bot"]` | never counted as answerers or feedback directors, whatever their association |
| `max_ci_attempts_per_pr` | `2` | delivered CI fix requests per PR, then `NEEDS_HUMAN` |
| `max_review_rounds_per_pr` | `4` | delivered review rounds per PR, then `NEEDS_HUMAN` |
| `spawn_reconcile_grace_minutes` | `20` | a claim without a live session inside this window is `PENDING_SPAWN`, not quarantined |
| `docker_test_wrapper` | `/home/ota/.ao/bin/mrmap-docker-test` | quoted into every worker prompt |
| `capacity_script` | `/home/ota/agent-orchestrator-test/mrmap/scripts/agent_capacity` | host gate consulted before a spawn; may be empty |
| `wip_script` | `/home/ota/agent-orchestrator-test/mrmap/scripts/wip` | run as `<wip_script> board` after claim-moving ticks (W5) |
| `board_issue` | `917` | work board issue: target of the W4 stall alert and of W3's terminated-claimant surfacing |
| `questions_enabled` | `report-only` | the W3 stage: `off` \| `report-only` \| `send` (see below) |
| `count_foreign_workers` | `true` | sessions not spawned by the controller still consume worker slots |
| `allow_restore_terminated` | `false` | AO does not restore a terminated session on Linux; stays false — a terminated claimant is surfaced, never resurrected |
| `use_thread_resolution` | `true` | resolved/outdated review threads are skipped, not delivered |
| `excluded_sessions` | `["mrmap-1"]` | never counted against `max_workers` (the orchestrator session) |
| `message_max_chars` | `12000` | worker messages are truncated at this |
| `quote_max_chars` | `700` | per review item quoted into a delivery |
| `command_timeout_seconds` | `120` | timeout for `gh`, `ao` and `wip` subprocess calls |

`approvers`, `review_allowlist`/`review_associations` and `ignore_authors` are
the whole trust model. A review from anyone else is logged and left for a human;
a question answer from anyone else never wakes a worker.

**Phantom keys removed by the truth pass:** `poll_seconds`, `intake_labels`,
`block_labels`, `min_seconds_between_dispatches`, `stale_after_hours`,
`capacity_command`, `ao.project`, `github.exclude_labels` were documented here
once and never existed in `config.py`. Anything not in the table above does not
exist, and `config.load` refuses the file when it appears.

---

## Install, configure, watch

```bash
cd ~/.ao/mrmap-controller
./install.sh                     # safe: checks, backups, installs units, does NOT enable
$EDITOR config.json
bin/mrmap-controller doctor      # every precondition: gh, AO, config, state, capacity, deny list
bin/mrmap-controller tick        # one dry run; prints what it would do
bin/mrmap-controller status      # what it currently believes, per issue
bin/mrmap-controller logs -n 40  # last structured events
bin/mrmap-controller reconcile-closing   # merged PRs whose issue is still open (W2)
```

A dry run reads everything and moves nothing: no spawn, no delivery, no claim
write. It writes its own `runs` ledger row and its bookkeeping marks only — the
pilot's `claims` table is byte-identical after a dry run, which
`tests/test_intake.py` asserts.

---

## Activation

### Loop start (verified — this is what actually runs here)

The loop process actually running was inspected through `/proc` on 2026-11-09
(pid `2489101`, recorded in `run/controller.pid`). Its real shape:

| Fact | Value |
| --- | --- |
| Command line | `/usr/bin/python3 -m mrmap_controller.cli loop --write-pidfile` |
| Interpreter | `/usr/bin/python3` (Python 3.13) |
| Working directory | `/home/ota/.ao/mrmap-controller` |
| stdout / stderr | both `logs/loop.log` (JSON lines from the stderr handler land here) |
| Start | `Fri Nov 6 15:37:01 2026`, detached (`setsid`, parent pid 1) |
| systemd | not a unit — this host has no systemd user bus; `systemd/*.unit` files are inert |

Behaviour-relevant environment (non-secret values, read from
`/proc/2489101/environ`):

- `MRMAP_CONTROLLER_CONFIG=/home/ota/.ao/mrmap-controller/config.json`
- `PYTHONPATH=/home/ota/.ao/mrmap-controller` (present twice: the wrapper appends
  it to an already-set value)
- Everything else is inherited from the operator's login shell; secret-looking
  values are deliberately not recorded here.

The verified restart sequence — after `bin/mrmap-controller stop` confirmed the
old loop is gone — is:

```bash
cd /home/ota/.ao/mrmap-controller
setsid ./bin/mrmap-controller loop --write-pidfile >> logs/loop.log 2>&1 &
```

which is exactly what `bin/mrmap-controller start` reproduces (it adds
`--dry-run` when `config.json` says `dry_run: true`). To activate without
`--dry-run`, set `"dry_run": false` in `config.json` first: `start` has no
`--execute` flag.

**Single instance (W4):** `loop` refuses to start while another loop holds the
flock on `<state_dir>/loop.lock`; the kernel drops that lock exactly when the
holder dies. The pidfile check remains as the friendly message. A loop already
running was started before the guard existed and is never touched by it.

### To turn it off

```bash
bin/mrmap-controller pause "reason"   # report-only, keeps ticking and logging
bin/mrmap-controller resume
bin/mrmap-controller stop             # SIGTERM to the pidfile pid; waits for the tick
```

`pause` is the switch to reach for first: a paused controller reports what it
would do, touches nothing — a paused controller will not interrupt a worker you
are watching — and the W3 stage likewise drops to report-only previews while
paused, deciding nothing (dry-run and paused previews do **not** consume a
question).

---

## What the controller may and may not do

| | |
| --- | --- |
| May | read GitHub; `ao spawn` / `ao send` / `ao session resume-agent`; post one comment via `gh issue comment` (the W4 stall alert, the W2 close ask, the W3 terminated-claimant surfacing); run `scripts/wip board`; write its own state database |
| Must not | merge, approve, close, resolve, push, edit GitHub labels or permissions, kill or restore a session, delete a volume, run any Docker command |

The prohibition is structural, not a prompt: every external call goes through a
runner with a deny list, so `gh pr merge`, `gh api -X POST`, `ao session kill`
and `docker …` fail inside the controller before they reach a binary. A worker
gets the same list as deny permissions on its `opencode` session and is
instructed to run heavy tests only through `mrmap-docker-test`.

**Merging and closing are the humans' jobs.** When CI is green and approved, the
issue moves to `ready_for_human_merge` and *stays claimed* — the controller
never releases a claim there and never merges. A claim is retired on `merged`
only, or released by a human through `release --confirm`.

---

## Behaviours added by the loop-integrity and QA packages

### W2 — closing-keyword reconciliation

The spawn prompt now requires the PR body to carry `Closes #<N>` (own line, the
actually assigned issue number) for default-branch PRs, and `Refs #<N>` plus a
stacking disclosure — never `Closes` — for stacked or non-default-base PRs. A
prose `( #N )` mention never closes an issue and is now only ever the DB-labeling
fallback.

On every merge observation the controller reads GitHub's **real closing
linkage** (`closedIssues`, the closed-references connection behind the merge
box's "closes #N" line). If the linkage did not fire and the issue is still
open, it posts **exactly one** comment on the issue naming the merged PR and
asking the human to close — the #981/#995 pattern made visible instead of
silently re-dispatchable. The controller never closes issues itself.

`bin/mrmap-controller reconcile-closing` prints the current human action list —
every claim retired as `merged` whose GitHub issue is still open — and mutates
nothing.

### W3 — question & answer lifecycle (`questions_enabled`)

One tick stage (`mrmap_controller/questions.py`) polls the comments of claimed
and tracked issues **and of their parent epics**, recognizing both machine
protocols verbatim: the `agent-question` marker of `scripts/wip ask` and the
`decision-request` marker of `scripts/decisions`. The stable identifiers are the
protocols' own ids (`id=0b014d9f`, `id=dr-85921e8f`); nothing is minted.

An answer counts only when it comes from a human account (not a Bot type, not on
`ignore_authors`) whose `author_association` is one of `review_associations`,
and never from a machine-managed comment (claim, question, board, request,
audit). Silence records nothing; partial answers never wake — a
decision-request wakes its epic's claimed children **and the child issues a
live worker session still carries** only when **all blocking
asks** are answered, otherwise a `QUESTION_GAP` line names what is missing. A
verified answer wakes the right worker through the normal AO layer (`ao send`,
resuming an exited agent first) with `scripts/wip answers && scripts/wip claim`
plus the verbatim answer and its permalink. A terminated claimant is never
resurrected: it is logged and surfaced with one comment on `board_issue`.

The rollout switch:

- `report-only` (shipped default): every wake decision is computed and **logged
  (`question_would_wake`) but nothing is sent**. A report is *not* a delivery:
  the decision is recorded under a separate `*_observed` event kind
  (`agent_question_observed`, `decision_request_observed`,
  `decision_child_wake_observed`) and the delivery kind stays free. So the
  switch can be finished safely — after a day of verified reports, flipping to
  `send` delivers those answers, once. Repeating a report-only pass neither
  re-logs nor re-reports an already observed decision.
- `send`: the decisions act (executing, unpaused ticks only) and write the
  delivery kinds (`agent_question`, `decision_request`,
  `decision_child_wake`), which close a decision in every mode.
- `off`: the stage does not run.

> **Review note (W3, 2026-11-09).** The first W3 implementation marked answers
> consumed in `report-only` too, which made the shipped default a one-way door:
> every question that arrived during the report-only rollout was burned without
> any worker being woken, and switching to `send` could never replay it. The
> observed/delivered split above is the fix; `tests/test_questions.py` pins both
> directions (report then send still delivers, and a delivered decision is
> never replayed by a later report-only pass). `scripts/wip answers` remains the
> worker's own fallback for pulling an answer, but it is no longer the only
> recovery path.

### W4 — watchdog, backoff, per-PR isolation

- Three consecutive aborted ticks post **one** comment on `board_issue` and one
  `CRITICAL loop_stalled` line; dedupe is per stall episode, a failed post
  retries next tick. Nothing is ever restarted.
- A controller-side failure while following one pull request is isolated to
  that pull request (`PR_FOLLOW_FAILED`); the rest of the tick continues. The
  retry window doubles from one tick interval, capped at 6 hours, and survives
  restarts (it lives in the dispatches table). A permanently poisoned PR keeps
  retrying at the cap until a human acts.

### W5 — board refresh

After any execute-mode tick that moved a claim (spawn, merge-retire,
quarantine), the controller runs `scripts/wip board`; the board's format stays
owned by `scripts/wip` and is duplicated nowhere. A failing `wip` run is a
WARNING (`board_refresh_failed`) and never costs a tick. Dry-run ticks never
touch the board.

### W6 — issue identity (`mrmap_controller/identity.py`)

One issue number is carried through the whole work item. For every start the
controller derives `ao/issue-<N>-<slug>` from the issue it was assigned, passes
it to `ao spawn --branch` **and** puts the same name in the spawn prompt, so the
workspace, the instruction and the claim cannot tell three stories.

A branch is only ever read back as a statement about an issue when a path
segment *starts* with `issue-<N>` (or `#<N>`) — the same rule `scripts/wip`
uses, deliberately byte-for-byte. A number buried in a prose slug declares
nothing: `ao/implement-github-issue-5/root` is what AO generated from the prompt
"Implement GitHub issue …#682", and reading issue #5 out of it is how claims and
a board row landed on issues closed in 2021. Names from before this change
(`ao/mrmap-77/root`, and the incident names above) declare nothing and keep
working; nothing is retro-renamed.

When a live session's branch declares a *different* issue than its assignment,
the controller refuses to pick a winner: the claim goes to
`needs_reconciliation`, the issue projects `failed`, and the action is
`NEEDS_HUMAN` with both numbers in the reason. A human decides which reading is
right — usually by fixing the branch, or by `release <issue> --confirm` to let
the issue be worked again.

---

## Recovery

### Controller crashed / machine rebooted

Nothing to decide: the loop is started again with the recorded command (or was
never down) and the first thing a tick does is reconcile. A row whose AO session
vanished goes to `needs_reconciliation` and is never auto-resumed; a claim whose
session still exists is adopted.

### A claim is stuck and you are sure the session is gone

```bash
bin/mrmap-controller status --json    # what it sees and why, per issue
bin/mrmap-controller backup           # snapshot first
bin/mrmap-controller release 120 --confirm
```

`release` refuses without `--confirm`. It is the only command that lets a second
worker be assigned to an issue. The row stays with state `released` as the audit
trail; the controller reads that as free, the pilot would read any row as held —
if you ever hand the issue back to the pilot, delete the row.

### You want to stop work on an issue for a while

```bash
gh issue edit 120 --add-label "agent: paused"    # the controller stops touching it
gh issue edit 120 --remove-label "agent: paused" # it picks the issue up again next tick
```

### The state database itself

```bash
bin/mrmap-controller backup     # → backups/state-<UTC>.sqlite3, 0600, online sqlite backup
```

Pre-parade snapshots from the November work packages are in `backups/` too:
`state-before-W1.sqlite3` (the consistent snapshot taken before the W1 schema
ALTER) and `pre-fixes-2026-11-09/` (the whole install tree; host has no rsync —
tar). To restore, the loop must be down, then:

```bash
bin/mrmap-controller stop; pgrep -f 'mrmap_controller.cli loop' || echo down
cp backups/state-before-W1.sqlite3 /home/ota/.ao/mrmap-gatekeeper/state.sqlite3
rm -f /home/ota/.ao/mrmap-gatekeeper/state.sqlite3-wal \
      /home/ota/.ao/mrmap-gatekeeper/state.sqlite3-shm   # sidecars of the replaced file
bin/mrmap-controller doctor
bin/mrmap-controller start
```

The schema repair is additive-only and forward-compatible: old code runs
against a migrated file (added columns carry defaults and the version stamp is
ignored by it), so a code rollback needs no database rollback.

### A worker was started but never appeared

`ao spawn` prints the new session; if that read fails the controller records
`SPAWN_UNCERTAIN` and will not start a second one. Run `status`: if you see no
session, `release` the issue and let intake retry; if you *can* see the session,
the next tick adopts it.

---

## Reading the log

```bash
bin/mrmap-controller logs -n 40
bin/mrmap-controller logs --level WARNING
grep -e '"event": "SPAWNED"' -e '"event": "DISPATCH_FAILED"' logs/controller.jsonl
```

Events worth watching: `SPAWNED` / `SPAWN_UNCERTAIN`, `DEFER` (capacity or the
host gate said no — a stream of these is not a dead loop, the counts ledger
proves liveness), `NEEDS_HUMAN` (quarantine, closed PR, exhausted retries,
terminated session), `MERGED` and `RECONCILE_CLOSING*` (W2), `DISPATCHED` /
`DISPATCH_FAILED`, `QUESTION_ANSWERED` / `QUESTION_GAP` /
`QUESTION_UNVERIFIED` / `question_would_wake` (W3), `loop_stalled` + a board
comment (W4), `PR_FOLLOW_FAILED` (isolated poison PR), `ABORTED` with
`tick_aborted`, `board_refresh_failed`.

---

## Host-side caveat (outside this install)

Worker prompts on this host have carried stale "run `ao review trigger`" text.
That text does **not** come from this controller (it exists nowhere in this
install); it comes from the AO harness's prompt/skill templates, e.g.
`~/.ao/data/prompts/<session>/system.md` and the worktree copy of
`.opencode/skills/using-ao/commands/review.md`. Cleaning it is an
orchestrator/host action outside this install; for this project AO native review
is off and was never triggered.

---

## Limits this build does not remove

- **No auto-merge, no auto-approval, no auto-close.** Not configurable, on
  purpose.
- **Capacity is conservative.** A claim holds its issue until merged; only
  executing sessions consume a slot, waking one counts.
- **A delivery that returns without an error is not proof.** AO reports the
  *previous* state for a wake, so the controller re-reads the session; an
  inconclusive read becomes `uncertain`: not retried, visible in `status`.
- **`ao pr watch` stays off.** The controller polls, which is restart-safe.
- AO's own automation ("MrMap GitHub Issue Intake") must stay off while the
  controller is on; `doctor` checks that exactly one scheduler is live.
