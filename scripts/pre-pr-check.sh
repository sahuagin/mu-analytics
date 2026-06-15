#!/usr/bin/env bash
# Pre-PR verification — mirrors .github/workflows/ci.yml.
#
# Runs ruff (format + lint), ty (types), the unittest suite, and verify-claims
# in sequence. Exits non-zero on first failure so the failing step is the last
# output. Prints elapsed time per step.
#
# Env:
#   PRE_PR_QUICK=1   skip the test suite (ruff + ty only) — fast inner loop
#   PRE_PR_NO_COLOR  disable color output
#   MU_SKIP_CLAIM_CHECK=1  bypass the verify-claims gate

set -u
set -o pipefail

# --- color setup -----------------------------------------------------------

if [ -t 1 ] && [ -z "${PRE_PR_NO_COLOR:-}" ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_DIM=""; C_OFF=""
fi

# --- locate repo root (jj workspaces have no top-level .git) ----------------

if command -v jj >/dev/null 2>&1 && jj root >/dev/null 2>&1; then
  REPO_ROOT="$(jj root)"
elif git rev-parse --show-toplevel >/dev/null 2>&1; then
  REPO_ROOT="$(git rev-parse --show-toplevel)"
else
  echo "${C_RED}pre-pr-check: not inside a jj or git repo${C_OFF}" >&2
  exit 2
fi
cd "$REPO_ROOT"

# Canonical interpreter (the pkg python3.11 with duckdb) — one source of truth.
PY="$(tq -f config.toml -r python_interpreter_path 2>/dev/null || echo python3)"

# --- step runner -----------------------------------------------------------

run_step() {
  local name="$1"; shift
  local start_s end_s elapsed
  start_s=$(date +%s)
  printf "%s==>%s %s\n" "$C_YELLOW" "$C_OFF" "$name"
  printf "%s    %s%s\n" "$C_DIM" "$*" "$C_OFF"
  if "$@"; then
    end_s=$(date +%s); elapsed=$((end_s - start_s))
    printf "%s    ok (%ds)%s\n\n" "$C_GREEN" "$elapsed" "$C_OFF"
  else
    local rc=$?; end_s=$(date +%s); elapsed=$((end_s - start_s))
    printf "%s    FAIL exit=%d (%ds)%s\n" "$C_RED" "$rc" "$elapsed" "$C_OFF"
    printf "%spre-pr-check: %s failed. Fix locally and re-run.%s\n" "$C_RED" "$name" "$C_OFF" >&2
    exit "$rc"
  fi
}

# --- checks ----------------------------------------------------------------

run_step "ruff format --check"  ruff format --check .
run_step "ruff check"           ruff check .
run_step "ty check"             ty check

if [ "${PRE_PR_QUICK:-}" = "1" ]; then
  printf "%s==> skipping tests (PRE_PR_QUICK=1)%s\n\n" "$C_DIM" "$C_OFF"
else
  run_step "unittest discover"  "$PY" -m unittest discover -s tests -v
fi

# verify-claims gate: every non-merge commit in main..@ (jj) / main..HEAD (git)
# must have a `## Files` block matching its diff. Opt-in: commits without the
# block pass. Bypass with MU_SKIP_CLAIM_CHECK=1.
verify_claims_step() {
  local check="$REPO_ROOT/scripts/verify-claims.sh"
  if [ ! -x "$check" ]; then
    printf "%s    verify-claims.sh missing — skipping%s\n\n" "$C_DIM" "$C_OFF"; return 0
  fi
  local commits=""
  if command -v jj >/dev/null 2>&1 && jj root >/dev/null 2>&1; then
    commits=$(jj log -r 'main..@ ~ empty() ~ merges()' --no-graph \
                --reversed -T 'commit_id ++ "\n"' 2>/dev/null || true)
  fi
  if [ -z "$commits" ]; then
    local base
    if base=$(git merge-base main HEAD 2>/dev/null); then
      commits=$(git rev-list --reverse --no-merges "$base..HEAD")
    fi
  fi
  if [ -z "$commits" ]; then
    printf "%s    no commits in main..@%s\n\n" "$C_DIM" "$C_OFF"; return 0
  fi
  local rc=0 c
  for c in $commits; do "$check" "$c" || rc=$?; done
  return "$rc"
}
run_step "verify-claims (main..@)" verify_claims_step

printf "%spre-pr-check: all checks green%s\n" "$C_GREEN" "$C_OFF"
