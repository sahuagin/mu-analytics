#!/bin/sh
# Daily incremental behavior-judge, run from cron. Judges ONLY cc transcripts that
# are new or have changed since the last run (judge_incremental.py diffs file mtime
# against the data/judge.sqlite ledger), so a day with no new sessions does no model
# work at all. Separate from refresh.sh on purpose: the 15-min dashboard refresh just
# READS the verdicts the judge writes here on its own slow clock.
#
# Install (on the host that has config.toml + a sqlite-capable python + ollama reach,
# i.e. the dashboard host). Self-locates the checkout (parent of ops/), so it survives
# syncs/relocations:
#   0 5 * * * /path/to/mu-analytics/ops/judge-cron.sh >> ~/mu-stats/judge.log 2>&1
#
# Overrides: MU_ANALYTICS_REPO (checkout path), JUDGE_LIMIT (cap sessions per run —
# leave unset for "all new"; set it for the first cold backfill to dip a toe).
set -u
# cron runs with a bare PATH; ./run needs tq, and run_judge.py needs agent-role /
# agent-dispatch / with-ollama-lease from ~/.local/bin.
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/bin:$PATH"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo="${MU_ANALYTICS_REPO:-$(cd "$script_dir/.." && pwd)}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] judge-cron"
if [ -n "${JUDGE_LIMIT:-}" ]; then
    exec "$repo/run" judge_incremental.py --limit "$JUDGE_LIMIT"
else
    exec "$repo/run" judge_incremental.py
fi
