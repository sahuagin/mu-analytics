# mu-analytics — common workflows.
#
# `just --list` shows everything without reading scripts/. Recipes are thin
# wrappers around ruff / ty / the test runner / the scripts — the front door,
# not the enforcement (the gates live in scripts/pre-pr-check.sh + ai-review.sh).
# Python port of the mu repo's justfile patterns.

# bash for the pr recipe's ${@:2} positional forwarding.
set shell := ["bash", "-cu"]

# Canonical interpreter: the pkg python3.11 that has duckdb. One source of truth
# (same value the ./run launcher resolves). Falls back to python3 off-host.
py := `tq -f config.toml -r python_interpreter_path 2>/dev/null || echo python3`

# Default recipe: list every available recipe (same as bare `just`).
list:
    @just --list

# ── pre-PR gate ────────────────────────────────────────────────────────────

# Full pre-PR check: ruff format + lint, ty, tests, verify-claims. Mirrors CI.
check:
    ./scripts/pre-pr-check.sh

# Quick pre-PR check: ruff + ty only (skip tests). Good for fast loops.
check-quick:
    PRE_PR_QUICK=1 ./scripts/pre-pr-check.sh

# Exactly what CI runs: fmt-check + lint + typecheck + test, fail-fast in CI order.
ci: fmt-check lint typecheck test

# Pre-PR cross-provider review panel (ports mu's gate): runs `just check` first,
# then two independent reviewers (local ollama + openrouter) inspect the diff,
# with a Claude tiebreaker on a split. Local only — needs the `mu` binary +
# provider auth. Verdict is read from reviewer stdout, not exit code. Override a
# REJECT with MU_REVIEW_OVERRIDE=1. See scripts/ai-review.sh.
ci-aipr: check
    scripts/ai-review.sh

# ── individual steps ───────────────────────────────────────────────────────

# Format every module in place.
fmt:
    ruff format .

# Check formatting without writing — same gate CI uses.
fmt-check:
    ruff format --check .

# Lint.
lint:
    ruff check .

# Type-check (Astral ty).
typecheck:
    ty check

# Unit tests — stdlib unittest (no install needed; pytest discovers them too).
test:
    {{py}} -m unittest discover -s tests -v

# ── dev / smoke ────────────────────────────────────────────────────────────

# Rebuild the dashboard from real data into dist/ (or a given path).
dash out="dist/index.html":
    ./run gen_dashboard.py {{out}}

# Full refresh into the nginx-served path (mu compact + cc + marks ingest + gen).
refresh:
    ./refresh.sh

# Print the assembled DATA contract (the dashboard's data, as JSON).
contract:
    ./run sample_data.py

# Smoke the DuckDB engine: the per-kind event histogram.
events:
    ./run engine.py

# Build a pyo3 parser crate from the mu repo into lib/ (private — use the named
# targets below). The .so is a gitignored build artifact: rerun after pulling mu
# changes. Override the mu checkout with MU_REPO=... if it isn't a sibling ../mu.
_build-pyo3 crate module:
    #!/bin/sh
    set -eu
    mu_repo="${MU_REPO:-$(cd "$(dirname "$(realpath justfile)")/../mu" && pwd)}"
    ext="$({{py}} -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX") or ".so")')"
    echo "==> cargo build --release -p {{crate}} (mu_repo=$mu_repo)"
    PYO3_PYTHON="{{py}}" cargo build --release --manifest-path "$mu_repo/Cargo.toml" -p {{crate}}
    mkdir -p lib
    cp "$mu_repo/target/release/lib{{module}}.so" "lib/{{module}}$ext"
    {{py}} -c "import sys; sys.path.insert(0, 'lib'); import {{module}}; print('OK: {{module}} -> lib/{{module}}$ext')"

# Typed Anthropic message parser — cc_telemetry.py's typed front door (cc_telemetry.py:40),
# vs the hand-rolled fallback.
build-anthropic-parser: (_build-pyo3 "mu-anthropic-py" "mu_anthropic_py")

# Typed SessionEvent read layer — analytics read typed, schema-validated events
# (scans.py / degradation) instead of re-parsing raw JSONL.
build-events-parser: (_build-pyo3 "mu-events-py" "mu_events_py")

# Build both typed pyo3 parsers into lib/.
build-parsers: build-anthropic-parser build-events-parser

# ── deploy ─────────────────────────────────────────────────────────────────

# Host running the dashboard cron (see "Deployment" in the README).
deploy_host := env_var_or_default("MU_ANALYTICS_HOST", "10.1.1.172")

# Deploy now, skipping the 15-min cron: SSH the host wrapper to sync to main + regenerate (details in README → Deployment).
deploy:
    ssh {{deploy_host}} /home/tcovert/mu-stats/mu-analytics-refresh.sh

# ── PR flow (jj-aware) ─────────────────────────────────────────────────────

# Bookmark current jj @ as <bookmark>, push, and open a PR. Extra args forward
# to `gh pr create` (e.g. --title ...). [positional-arguments] preserves quoting.
[positional-arguments]
pr bookmark *gh_args:
    @echo "==> bookmark $1 on @ → push → gh pr create"
    jj bookmark create "$1" -r @ 2>/dev/null || jj bookmark set "$1" -r @
    jj git push --bookmark "$1"
    gh pr create --base main --head "$1" "${@:2}"
