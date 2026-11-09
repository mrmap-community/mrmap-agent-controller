#!/usr/bin/env bash
# Install or upgrade the MrMap Agent Controller on this host.
#
# What it does:    checks, config, state database backup, unit files, doctor run.
# What it never does: start or enable a scheduler, touch GitHub labels or
#                    permissions, modify the pilot gatekeeper, run Docker cleanup.
#
#   ./install.sh                 install or update files, back up state, report
#   ./install.sh --enable        also enable the scheduler (systemd timer, or the
#                                detached loop on a host without systemd)
#   ./install.sh --execute       also switch config dry_run to false (implies --enable
#                                is still a separate, deliberate act)
#   ./install.sh --test          run the unit test suite before anything else
set -euo pipefail

# install.sh sits in the install root next to bin/, config.example.json and systemd/,
# so its own directory *is* the installation. (bin/mrmap-controller resolves one level up.)
SELF="$(readlink -f "$0")"
INSTALL="$(cd "$(dirname "$SELF")" && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
ENABLE=0
RUN_TESTS=0
WANT_EXECUTE=0
CONFIG="${MRMAP_CONTROLLER_CONFIG:-$INSTALL/config.json}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --enable) ENABLE=1 ;;
        --execute) WANT_EXECUTE=1 ;;
        --test) RUN_TESTS=1 ;;
        --config) CONFIG="$2"; shift ;;
        -h|--help) sed -n '1,16p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAILED: %s\n' "$*" >&2; exit 1; }

step "checking the host"
command -v python3 >/dev/null || fail "python3 is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || fail "python 3.9 or newer is required"
command -v gh >/dev/null || fail "the GitHub CLI is required"
command -v ao >/dev/null || fail "the AO CLI is required"
gh auth status >/dev/null 2>&1 || fail "gh is not authenticated; log in first"
[[ -x /home/ota/.ao/bin/mrmap-docker-test ]] \
    || echo "WARNING: the Docker test wrapper is missing; workers will not be able
         to run Docker suites safely" >&2

step "configuration: $CONFIG"
if [[ ! -f "$CONFIG" ]]; then
    cp "$INSTALL/config.example.json" "$CONFIG"
    echo "created $CONFIG from config.example.json (dry_run=true, max_workers=1)"
fi
[[ -f "$CONFIG" ]] || fail "config not found: $CONFIG"
chmod 600 "$CONFIG"
if [[ $WANT_EXECUTE -eq 1 ]]; then
    python3 - "$CONFIG" <<'PY'
import json, sys
path = sys.argv[1]
data = json.loads(open(path).read())
data['dry_run'] = False
open(path, 'w').write(json.dumps(data, indent=2) + '\n')
print("dry_run is now false: ticks may start workers and deliver messages")
PY
fi

step "running the test suite"
if [[ $RUN_TESTS -eq 1 ]]; then
    (cd "$INSTALL" && python3 -m unittest discover -s tests -t . -v) \
        || fail "unit tests failed; nothing was changed"
else
    echo "skipped (use --test to run them)"
fi

step "backing up the state database"
PYTHONPATH="$INSTALL" MRMAP_CONTROLLER_CONFIG="$CONFIG" \
    "$INSTALL/bin/mrmap-controller" backup \
    || echo "WARNING: no state database to back up yet" >&2

step "installing systemd units (if this host has a user manager)"
if systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$UNIT_DIR"
    for unit in mrmap-controller.service mrmap-controller.timer; do
        sed "s|@INSTALL@|$INSTALL|g" "$INSTALL/systemd/$unit" \
            > "$UNIT_DIR/$unit"
        echo "wrote $UNIT_DIR/$unit"
    done
    systemctl --user daemon-reload
    HAVE_SYSTEMD=1
else
    # This host has no systemd user manager (common inside a container): the
    # equivalent is the controller's own supervised loop, which takes the same
    # exclusive lock a tick would, so ticks can never overlap.
    HAVE_SYSTEMD=0
    echo "no systemd user instance: the units are ready in $INSTALL/systemd for a"
    echo "systemd host; on this host use the loop instead (see the end of this output)"
fi

step "doctor"
PYTHONPATH="$INSTALL" MRMAP_CONTROLLER_CONFIG="$CONFIG" \
    "$INSTALL/bin/mrmap-controller" doctor || fail "doctor reported problems"

step "checking that AO runs no competing scheduler"
if command -v ao >/dev/null; then
    ENABLED_AUTOMATIONS="$(ao automation list --json 2>/dev/null \
        | python3 -c 'import json,sys
try:
    rows = json.load(sys.stdin).get("automations") or []
except Exception:
    rows = []
print(", ".join(r.get("displayName") or r.get("id") or "?" for r in rows
                if r.get("enabled")))')"
    if [[ -n "$ENABLED_AUTOMATIONS" ]]; then
        fail "AO automation(s) are enabled ($ENABLED_AUTOMATIONS). Two schedulers
              would assign the same issue twice. Disable them with
              'ao automation delete <id>' or keep this controller disabled."
    fi
    echo "no AO automation is enabled: this controller will be the only scheduler"
fi

if [[ $ENABLE -eq 0 ]]; then
    step "installation complete, scheduler NOT enabled"
    cat <<EOF
Nothing is running yet. Ticks happen only when you run:
    $INSTALL/bin/mrmap-controller tick            # dry run, prints what it would do
    $INSTALL/bin/mrmap-controller tick --execute  # acts, within the config limits

To start the schedule deliberately:
    $0 --enable                  $( [[ $HAVE_SYSTEMD -eq 1 ]] \
        && echo 'enables the five-minute systemd timer' \
        || echo 'starts the supervised loop (no systemd here)' )

Before enabling, review the two switches in $CONFIG:
    dry_run        true keeps every tick report-only
    max_workers    1 during rollout, 3 is the host cap
EOF
    exit 0
fi

if [[ $HAVE_SYSTEMD -eq 1 ]]; then
    step "enabling the five-minute timer"
    systemctl --user enable --now mrmap-controller.timer
    systemctl --user --no-pager list-timers mrmap-controller.timer || true
    echo "stop again with: systemctl --user disable --now mrmap-controller.timer"
else
    step "starting the supervised loop"
    PYTHONPATH="$INSTALL" MRMAP_CONTROLLER_CONFIG="$CONFIG" \
        "$INSTALL/bin/mrmap-controller" start
    echo "stop again with: $INSTALL/bin/mrmap-controller stop"
fi

step "status"
PYTHONPATH="$INSTALL" MRMAP_CONTROLLER_CONFIG="$CONFIG" \
    "$INSTALL/bin/mrmap-controller" status
