#!/bin/sh
# Cron wrapper for the mu-analytics hourly refresh.
#
# Why this exists: the cron runs refresh.sh out of the SHARED dev working copy.
# On 2026-06-15 that checkout was parked on a pre-WS3 feature commit, so the
# hourly gen_dashboard rendered mu-only and clobbered the cc behavioral panels.
# This wrapper keeps the checkout on merged code: if the working copy is CLEAN,
# fast-forward it to origin/main before refreshing. If it's DIRTY (a dev session
# is mid-work), skip the sync and just refresh — never clobber in-progress work.
set -u
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
repo=/home/tcovert/src/public_github/mu-analytics

# Relay live claude-code + mu logs into ~/ai-sessions BEFORE the dashboard reads
# it, so the page never renders a stale archive. All sources are local (the aiteam
# "host" is an iocage jail here). Non-fatal: a relay hiccup must not block refresh.
ai-sessions-sync >> "$HOME/mu-stats/ai-sessions-sync.log" 2>&1 || true

# `empty`==true means the working-copy commit has no changes (clean tree).
clean=$(jj -R "$repo" log --no-graph -r @ -T 'empty' 2>/dev/null)
if [ "$clean" = "true" ]; then
    jj -R "$repo" git fetch -q 2>/dev/null || true
    new=$(jj -R "$repo" log --no-graph -r 'main@origin' -T 'commit_id.short()' 2>/dev/null)
    cur=$(jj -R "$repo" log --no-graph -r '@-'          -T 'commit_id.short()' 2>/dev/null)
    # Only move when main actually advanced — avoids churning an empty commit hourly.
    if [ -n "$new" ] && [ "$new" != "$cur" ]; then
        jj -R "$repo" new 'main@origin' 2>/dev/null || true
    fi
fi

exec "$repo/refresh.sh"
