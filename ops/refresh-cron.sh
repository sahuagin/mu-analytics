#!/bin/sh
# Periodic refresh wrapper for the mu-analytics dashboard, run from cron.
#
# Install (per host): point cron at THIS file inside the checkout, e.g.
#   */15 * * * * /path/to/mu-analytics/ops/refresh-cron.sh >> ~/mu-stats/cron.log 2>&1
# It self-locates the repo (parent of ops/), so it survives syncs/relocations.
# Overrides: MU_ANALYTICS_REPO (checkout path), MU_ANALYTICS_STATE (logs/runtime
# dir, default ~/mu-stats). Cadence is set by the crontab line, not here.
#
# --sync-only: fast-forward the checkout to main@origin under the same cycle
# lock, print what happened, exit — no relay, no refresh. `just deploy` runs
# this, so shipping code never starts a refresh outside cron's clock; the next
# cron cycle regenerates on the new code. Waits up to 30 min for a running cycle.
#
# Why it exists: cron regenerates the dashboard from the SHARED dev checkout. On
# 2026-06-15 that checkout sat on a pre-WS3 feature commit, so gen_dashboard
# rendered mu-only and clobbered the cc behavioral panels. This wrapper keeps the
# checkout on merged code: if the working copy is CLEAN, fast-forward it to
# origin/main before refreshing; if DIRTY (a dev session is mid-work), skip the
# sync and just refresh — never clobber in-progress work.
set -u
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

sync_only=0
case "${1:-}" in --sync-only) sync_only=1 ;; esac

# Self-locate the checkout (this script lives at <repo>/ops/refresh-cron.sh) so
# the wrapper isn't pinned to one host's path; MU_ANALYTICS_REPO overrides.
# Resolve symlinks first: a crontab may point at a link elsewhere (e.g. the old
# ~/mu-stats wrapper path), and $0 is the link, not the file.
self=$(readlink -f -- "$0" 2>/dev/null || realpath -- "$0" 2>/dev/null || printf '%s' "$0")
script_dir=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd)
repo="${MU_ANALYTICS_REPO:-$(cd "$script_dir/.." && pwd)}"
state="${MU_ANALYTICS_STATE:-$HOME/mu-stats}"
mkdir -p "$state"

# One cycle at a time (mu-refresh-saturation-xycf item 3). refresh.sh has its
# own pidfile guard, but it fires AFTER the relay walk below — and the walk is
# most of a slot's cost (~14 min against a 15-min cadence), so an overlapping
# cron fire used to pay the whole walk just to be told to skip. Re-exec under a
# non-blocking lock so an overlapping fire logs one line and exits up front.
# Lock-held exits 75 (EX_TEMPFAIL: lockf's native code; flock told via -E).
# Hosts with neither tool fall through unlocked, as before.
# A deploy (--sync-only) waits for the lock instead of skipping: it must not
# move the checkout under a running cycle, and it must not silently do nothing.
if [ -z "${MU_ANALYTICS_CRON_LOCKED:-}" ]; then
    lock="$state/refresh-cron.lock"
    wait=0
    [ "$sync_only" = 1 ] && wait=1800
    rc=""
    if command -v lockf >/dev/null 2>&1; then
        MU_ANALYTICS_CRON_LOCKED=1 lockf -st "$wait" "$lock" "$0" "$@"
        rc=$?
    elif command -v flock >/dev/null 2>&1; then
        MU_ANALYTICS_CRON_LOCKED=1 flock -w "$wait" -E 75 "$lock" "$0" "$@"
        rc=$?
    fi
    if [ -n "$rc" ]; then
        if [ "$rc" -eq 75 ]; then
            if [ "$sync_only" = 1 ]; then
                echo "[$(ts)] deploy: a refresh cycle is still running after ${wait}s; checkout not moved" >&2
                exit 75
            fi
            echo "[$(ts)] refresh-cron skipped: previous cycle still active"
            exit 0
        fi
        exit "$rc"
    fi
fi

# Relay live claude-code + mu logs into ~/ai-sessions BEFORE the dashboard reads
# them, so the page never renders a stale archive. Prefer the versioned relay
# next to this wrapper (ops/ai-sessions-sync) so its rsync flags ship with the
# checkout; fall back to one on PATH. Non-fatal and skipped where neither
# exists: a relay hiccup must not block the refresh.
relay="$script_dir/ai-sessions-sync"
if [ "$sync_only" = 1 ]; then
    :   # deploy ships code only; the relay is data and runs on cron's clock
elif [ -x "$relay" ]; then
    "$relay" >> "$state/ai-sessions-sync.log" 2>&1 || true
elif command -v ai-sessions-sync >/dev/null 2>&1; then
    ai-sessions-sync >> "$state/ai-sessions-sync.log" 2>&1 || true
fi

# `empty`==true means the working-copy commit has no changes (clean tree).
clean=$(jj -R "$repo" log --no-graph -r @ -T 'empty' 2>/dev/null)
cur=$(jj -R "$repo" log --no-graph -r '@-' -T 'commit_id.short()' 2>/dev/null)
if [ "$clean" = "true" ]; then
    jj -R "$repo" git fetch -q 2>/dev/null || true
    new=$(jj -R "$repo" log --no-graph -r 'main@origin' -T 'commit_id.short()' 2>/dev/null)
    # Only move when main actually advanced — avoids churning an empty commit each run.
    if [ -n "$new" ] && [ "$new" != "$cur" ]; then
        if jj -R "$repo" new 'main@origin' >/dev/null 2>&1; then
            outcome="checkout fast-forwarded $cur -> $new"
        else
            outcome="jj new main@origin FAILED; checkout left at $cur"
        fi
    else
        outcome="checkout already at main@origin ($cur)"
    fi
else
    outcome="working copy dirty; checkout left at $cur"
fi

if [ "$sync_only" = 1 ]; then
    echo "[$(ts)] deploy: $outcome"
    exit 0
fi
case "$outcome" in *fast-forwarded*|*FAILED*) echo "[$(ts)] $outcome" ;; esac

exec "$repo/refresh.sh"
