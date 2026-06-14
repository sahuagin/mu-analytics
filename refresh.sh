#!/bin/sh
# Full dashboard refresh for cron: re-project both fleets into their sinks, then
# regenerate the dashboard into the nginx-served path (paths.dashboard_out).
# Each step is non-fatal — a transient failure logs a warning but still lets the
# page regenerate rather than going stale. Logs to wherever cron redirects it.
#
# cron:
#   @hourly $HOME/src/mu-analytics/refresh.sh >> $HOME/mu-stats/cron.log 2>&1
set -u
# cron runs with a bare PATH; tq lives in ~/.cargo/bin, mu in ~/.local/bin.
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mu="$HOME/.local/bin/mu"
out=$(tq -f "$here/config.toml" -r paths.dashboard_out)
cc_events=$(tq -f "$here/config.toml" -r paths.cc_events_out)
cc_sink=$(tq -f "$here/config.toml" -r paths.cc_sink_db)

echo "[$(date '+%Y-%m-%d %H:%M:%S')] mu-analytics refresh"
# mu sink: project mu's own events (idempotent upsert into telemetry.sqlite)
"$mu" analytics compact >/dev/null 2>&1 || echo "  warn: mu compact failed"
# cc sink: re-emit all cc accounts -> events -> compact
"$here/run" cc_telemetry.py >/dev/null 2>&1 || echo "  warn: cc emit failed"
"$mu" analytics compact --events-dir "$cc_events" --db "$cc_sink" >/dev/null 2>&1 || echo "  warn: cc compact failed"
# regenerate the dashboard into the served path
"$here/run" gen_dashboard.py "$out" || echo "  warn: dashboard gen failed"
