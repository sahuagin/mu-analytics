# Upgrading the analytics host

How to bring the dashboard host (`just deploy`'s target, default `10.1.1.172`)
up to what `main` needs: the pkg python and compiled deps, the deployed code, and
the pyo3 bindings in `lib/`. Run it after a merge that changes `pyproject.toml`'s
pins, the seed python in `config.toml`, or the mu crates the bindings build from.

## What the host needs

- **pkg python**: the seed in `config.toml` (`python_interpreter_path`,
  currently `/usr/local/bin/python3.12`). The venv is built from it.
- **pkg py312 packages**:
  - `py312-duckdb`, matching the `pyproject.toml` pin (2.0.0);
  - `py312-numpy` and `py312-scikit-learn` (`degradation.py`, run every refresh);
  - `py312-polars` (`cost.py`) and `py312-requests` (`admin_usage.py`, the cost panel);
  - `py312-sqlite3` (FreeBSD ships the stdlib sqlite module separately; the sinks use it).
- **Tools**: `tq`, `uv`, `cargo`/rustc, `jj`, and `~/.local/bin/mu`
  (`refresh.sh` runs `mu analytics compact`).
- **A mu checkout on main**: `lib/` holds three bindings built from it
  (`mu_anthropic_py`, `mu_events_py`, `mu_bridge`). `session_health.py` needs
  `mu_events_py.diagnose_session`.

No data migration is needed for a duckdb version change: the engine connects
in memory, and its one on-disk snapshot is parquet (`data/ev-snapshot.parquet`).

## 1. Check the host (read-only)

```sh
ssh -A 10.1.1.172
cd ~/src/public_github/mu-analytics
tq -f config.toml -r python_interpreter_path
.venv/bin/python -c 'import sys, duckdb; print(sys.version.split()[0], duckdb.__version__, duckdb.__file__)'
pkg query '%n %v' python312 py312-duckdb py312-numpy py312-scikit-learn py312-polars py312-requests py312-sqlite3
sudo pkg update -f && pkg rquery '%n %v' py312-duckdb
```

If `duckdb.__file__` is inside `.venv/`, the venv has its own copy that hides
pkg's; step 4 removes it.

## 2. Pause the refresh

```sh
lockf -t 1800 ~/mu-stats/refresh-cron.lock /bin/sh
```

This shell holds the lock `ops/refresh-cron.sh` takes. Cron fires while it is
held log "skipped" and exit, so no cycle renders from a half-upgraded host. Do
steps 3 to 6 inside it.

## 3. Upgrade the packages

```sh
sudo pkg upgrade py312-duckdb python312 py312-numpy py312-scikit-learn py312-polars py312-requests py312-sqlite3
```

If the catalog doesn't offer the pinned duckdb yet, install it into the venv
from a wheelfs wheel instead (wheelfs: `~/src/public_github/wheelfs`,
`cargo build --release`):

```sh
# py312-duckdb-<ver>.pkg from https://pkg.freebsd.org/FreeBSD:15:amd64/latest/All/ into ~/pkgs
wheelfs materialize duckdb -o ~/wheels --pkg-dir ~/pkgs
uv pip install --python .venv/bin/python --no-index --find-links ~/wheels duckdb==2.0.0
```

## 4. Check the venv

- Python minor version unchanged: keep the venv; it sees pkg's upgraded
  packages through `--system-site-packages`.
- Python minor version changed: update `python_interpreter_path` in
  `config.toml`, then rebuild the venv:

  ```sh
  rm -rf .venv
  uv venv --python "$(tq -f config.toml -r python_interpreter_path)" --system-site-packages --no-python-downloads .venv
  ```

- Step 1 found a duckdb inside the venv that you didn't put there on purpose:
  `uv pip uninstall --python .venv/bin/python duckdb`.

## 5. Deploy the code

```sh
MU_ANALYTICS_CRON_LOCKED=1 ops/refresh-cron.sh --sync-only
```

This is what `just deploy` runs. The variable tells the script the lock is
already held (by step 2's shell), so it doesn't wait on itself. It fetches with
your forwarded agent (`ssh -A`). Then bring the mu checkout to main:

```sh
cd <mu checkout> && jj git fetch && jj new main@origin && cd -
```

## 6. Rebuild the bindings and verify

```sh
MU_REPO=<mu checkout> just py=$PWD/.venv/bin/python build-parsers   # prints OK: <module> for each
.venv/bin/python -c 'import duckdb; print(duckdb.__version__)'       # the pinned version
.venv/bin/python -c 'import numpy, sklearn, polars, requests, sqlite3; print("ok")'
./run engine.py                                                     # event histogram
./run admin_usage.py                                                # "ok": true
./run session_health.py                                             # writes ~/mu-stats/recover-worklist.md
```

`session_health.py` reads the archive on the host and sends nothing anywhere.

## 7. Resume

Exit step 2's shell. Watch the next cycle finish in `~/mu-stats/cron.log` and
check the dashboard's as-of time moves.

## Rollback

Reinstall the previous pkg (`sudo pkg install -f py312-duckdb-<old>`, or the
`.pkg` left in `/var/cache/pkg`). If the change being rolled back came with
code that needs the new version, also check out the previous commit and rerun
step 6's `build-parsers`.
