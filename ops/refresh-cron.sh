#!/bin/sh
# Periodic refresh wrapper for the mu-analytics dashboard, run from cron.
#
# Install (per host): point cron at THIS file inside the checkout, e.g.
#   */15 * * * * /path/to/mu-analytics/ops/refresh-cron.sh >> ~/mu-stats/cron.log 2>&1
# It self-locates the repo (parent of ops/), so it survives syncs/relocations.
# Overrides: MU_ANALYTICS_REPO (checkout path), MU_ANALYTICS_STATE (logs/runtime
# dir, default ~/mu-stats). Cadence is set by the crontab line, not here.
#
# Why it exists: cron regenerates the dashboard from the SHARED dev checkout. On
# 2026-06-15 that checkout sat on a pre-WS3 feature commit, so gen_dashboard
# rendered mu-only and clobbered the cc behavioral panels. This wrapper keeps the
# checkout on merged code: if the working copy is CLEAN, fast-forward it to
# origin/main before refreshing; if DIRTY (a dev session is mid-work), skip the
# sync and just refresh — never clobber in-progress work.
set -u
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/bin:$PATH"

# Self-locate the checkout (this script lives at <repo>/ops/refresh-cron.sh) so
# the wrapper isn't pinned to one host's path; MU_ANALYTICS_REPO overrides.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo="${MU_ANALYTICS_REPO:-$(cd "$script_dir/.." && pwd)}"
state="${MU_ANALYTICS_STATE:-$HOME/mu-stats}"
mkdir -p "$state"

# Relay live claude-code + mu logs into ~/ai-sessions BEFORE the dashboard reads
# them, so the page never renders a stale archive. Host-specific and non-fatal:
# skipped where the relay isn't installed; a hiccup must not block the refresh.
if command -v ai-sessions-sync >/dev/null 2>&1; then
    ai-sessions-sync >> "$state/ai-sessions-sync.log" 2>&1 || true
fi

# `empty`==true means the working-copy commit has no changes (clean tree).
clean=$(jj -R "$repo" log --no-graph -r @ -T 'empty' 2>/dev/null)
if [ "$clean" = "true" ]; then
    jj -R "$repo" git fetch -q 2>/dev/null || true
    new=$(jj -R "$repo" log --no-graph -r 'main@origin' -T 'commit_id.short()' 2>/dev/null)
    cur=$(jj -R "$repo" log --no-graph -r '@-'          -T 'commit_id.short()' 2>/dev/null)
    # Only move when main actually advanced — avoids churning an empty commit each run.
    if [ -n "$new" ] && [ "$new" != "$cur" ]; then
        jj -R "$repo" new 'main@origin' 2>/dev/null || true
    fi
fi

exec "$repo/refresh.sh"
