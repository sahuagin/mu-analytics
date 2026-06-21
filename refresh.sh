#!/bin/sh
# Full dashboard refresh for cron: re-project both fleets into their sinks, then
# regenerate the dashboard into the nginx-served path (paths.dashboard_out).
# Each step is non-fatal — a transient failure logs a warning but still lets the
# page regenerate rather than going stale. Logs to wherever cron redirects it.
#
# cron:
#   */15 * * * * /path/to/mu-analytics/refresh.sh >> /path/to/cron.log 2>&1
set -u
# cron runs with a bare PATH; tq lives in ~/.cargo/bin, mu in ~/.local/bin.
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mu="$HOME/.local/bin/mu"
out=$(tq -f "$here/config.toml" -r paths.dashboard_out)
cc_events=$(tq -f "$here/config.toml" -r paths.cc_events_out)
cc_sink=$(tq -f "$here/config.toml" -r paths.cc_sink_db)
mu_sink=$(tq -f "$here/config.toml" -r paths.mu_sink_db)
mu_events_root=$(tq -f "$here/config.toml" -r paths.mu_events_root)
pidfile="${TMPDIR:-/tmp}/mu-analytics-refresh.pid"

now_s() { date +%s; }
log() { echo "  $*"; }

if [ -f "$pidfile" ]; then
  oldpid=$(cat "$pidfile" 2>/dev/null || true)
  if [ -n "${oldpid:-}" ] && kill -0 "$oldpid" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] mu-analytics refresh skipped: previous run still active (pid $oldpid)"
    exit 0
  fi
  log "removing stale pidfile $pidfile"
  rm -f "$pidfile"
fi

printf '%s\n' "$$" >"$pidfile"
cleanup() { rm -f "$pidfile"; }
trap cleanup EXIT HUP INT TERM

run_step() {
  name=$1
  shift
  start=$(now_s)
  if "$@" >/dev/null 2>&1; then
    status=ok
  else
    status=warn
  fi
  dur=$(( $(now_s) - start ))
  if [ "$status" = ok ]; then
    log "$name ok (${dur}s)"
  else
    log "warn: $name failed (${dur}s)"
  fi
}

start_total=$(now_s)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] mu-analytics refresh"
# mu sink: project every machine's mu events from the consolidated archive
#   <mu_events_root>/<machine>/events/<daemon>/*.jsonl  ->  mu_sink (idempotent upsert).
# Loop because `mu analytics compact --events-dir` wants ONE dir whose immediate
# children are daemon dirs; the archive interposes a per-machine level above that.
for evdir in "$mu_events_root"/*/events; do
  [ -d "$evdir" ] || continue
  machine=$(basename "$(dirname "$evdir")")
  run_step "mu compact [$machine]" "$mu" analytics compact --events-dir "$evdir" --db "$mu_sink"
done
# cc sink: re-emit all cc accounts -> events -> compact
run_step "cc emit" "$here/run" cc_telemetry.py
run_step "cc compact" "$mu" analytics compact --events-dir "$cc_events" --db "$cc_sink"
# fold any dashboard-exported marks (data/marks_inbox/*.jsonl) into marks.sqlite
run_step "marks ingest" "$here/run" marks_store.py ingest
# degradation probe + mu-audit findings -> the data the dashboard's degradation
# section folds in (replaces the old degradation_ml.py -> mu_audit_sweep.py chain).
run_step "degradation probe" "$here/run" degradation.py
run_step "audit sweep" "$here/run" audit_sweep.py
# regenerate the dashboard into the served path. Keep its summary visible.
gen_start=$(now_s)
if "$here/run" gen_dashboard.py "$out"; then
  log "dashboard gen ok ($(( $(now_s) - gen_start ))s)"
else
  log "warn: dashboard gen failed ($(( $(now_s) - gen_start ))s)"
fi
log "total duration $(( $(now_s) - start_total ))s"
