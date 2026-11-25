# Upgrading the analytics host

How to bring the dashboard host (`just deploy`'s target, default `10.1.1.172`)
up to what `main` needs: the pkg python and compiled deps, the deployed code, and
the pyo3 bindings in `lib/`. Run it after a merge that changes `pyproject.toml`'s
pins, the seed python in `config.toml`, or the mu crates the bindings build from.

Do it as the account that owns the deploy checkout and runs the refresh cron.
Other accounts on the host may have their own mu-analytics checkout; cron
doesn't use those.

The usual upgrade is steps 1 to 7 in order, with nothing to choose along the
way. The cases that need something extra are under "Only if" below; step 1
tells you whether any of them apply.

## What the host needs

- **pkg python**: the seed in `config.toml` (`python_interpreter_path`,
  currently `/usr/local/bin/python3.12`). The venv is built from it.
- **pkg py312 packages**:
  - `py312-duckdb`, matching the `pyproject.toml` pin (2.0.0);
  - `py312-numpy` and `py312-scikit-learn` (`degradation.py`, run every refresh);
  - `py312-polars` (`cost.py`) and `py312-requests` (`admin_usage.py`, the cost panel);
  - `py312-sqlite3` (FreeBSD ships the stdlib sqlite module separately; the sinks use it).
- **Tools**: `tq`, `cargo`/rustc, `jj`, and `~/.local/bin/mu`
  (`refresh.sh` runs `mu analytics compact`).
- **A mu checkout on main**: `lib/` holds three bindings built from it
  (`mu_anthropic_py`, `mu_events_py`, `mu_bridge`). `session_health.py` needs
  `mu_events_py.diagnose_session`.

The venv installs nothing itself. It is built with `--system-site-packages`, so
every package comes from pkg; `./run` requires it so the interpreter binding is
explicit. No data migration is needed for a duckdb version change: the engine
connects in memory, and its one on-disk snapshot is parquet
(`data/ev-snapshot.parquet`).

## 1. Check the host (read-only)

```sh
ssh -A 10.1.1.172
cd ~/src/public_github/mu-analytics
tq -f config.toml -r python_interpreter_path
.venv/bin/python -c 'import sys, duckdb; print(sys.version.split()[0], duckdb.__version__, duckdb.__file__)'
pkg query '%n %v' python312 py312-duckdb py312-numpy py312-scikit-learn py312-polars py312-requests py312-sqlite3
sudo pkg update -f && pkg rquery '%n %v' py312-duckdb
```

The usual answers: the venv's python has the same minor version as the seed,
`duckdb.__file__` is under `/usr/local/lib/python3.12/site-packages`, and
`pkg rquery` offers the pinned duckdb. If all three hold, go straight through
steps 2 to 7. If one doesn't, read the matching "Only if" section first.

## 2. Pause the refresh

```sh
lockf -t 1800 ~/mu-stats/refresh-cron.lock /bin/sh
```

This shell holds the lock `ops/refresh-cron.sh` takes. Cron fires while it is
held log "skipped: previous cycle still active" and exit, so no cycle renders
from a half-upgraded host. Do steps 3 to 6 inside it, and exit it promptly
after: refreshes stay paused for as long as it is open.

If you leave the shell early, run each remaining step under the lock instead:
`lockf -t 1800 ~/mu-stats/refresh-cron.lock sh -c '<command>'`. Step 5's
binding rebuild matters most here, since it overwrites `.so` files a running
cycle may have loaded.

## 3. Upgrade the packages

```sh
sudo pkg upgrade py312-duckdb python312 py312-numpy py312-scikit-learn py312-polars py312-requests py312-sqlite3
```

The venv sees the new versions immediately; nothing else needs installing.

## 4. Deploy the code

```sh
MU_ANALYTICS_CRON_LOCKED=1 ops/refresh-cron.sh --sync-only
cd <mu checkout> && jj git fetch && jj new main@origin && cd -
```

The first line is what `just deploy` runs. The variable tells the script the
lock is already held (by step 2's shell), so it doesn't wait on itself. It
fetches with your forwarded agent (`ssh -A`). The second line brings the mu
checkout the bindings build from to main.

## 5. Rebuild the bindings

```sh
MU_REPO=<mu checkout> just py=$PWD/.venv/bin/python build-parsers   # prints OK: <module> for each
```

## 6. Verify

```sh
.venv/bin/python -c 'import duckdb; print(duckdb.__version__)'       # the pinned version
.venv/bin/python -c 'import numpy, sklearn, polars, requests, sqlite3; print("ok")'
./run engine.py                                                     # event histogram
./run admin_usage.py                                                # "ok": true
./run session_health.py                                             # writes ~/mu-stats/recover-worklist.md
```

`session_health.py` reads the archive on the host and sends nothing anywhere.

## 7. Resume

Exit step 2's shell. The next cron fire runs a full cycle: watch it finish in
`~/mu-stats/cron.log` (each step logs `ok`, then `total duration`) and check
the dashboard's as-of time moves.

## Only if

### The venv's python minor version differs from the seed

pkg moved to a new python. Update `python_interpreter_path` in `config.toml` if
needed, then rebuild the venv between steps 3 and 4:

```sh
rm -rf .venv
"$(tq -f config.toml -r python_interpreter_path)" -m venv --system-site-packages --without-pip .venv
```

Step 5 then builds the bindings for the new ABI.

### `duckdb.__file__` is inside `.venv/`

Something installed a copy of duckdb into the venv, and it hides pkg's. Remove
it after step 3 (unless it is the wheelfs copy from the next section):

```sh
uv pip uninstall --python .venv/bin/python duckdb
```

### pkg doesn't offer the pinned duckdb yet

The code on main may still run on the older duckdb; check the pin's commit
before deciding to wait for the catalog. To install the pinned version now,
convert the pkg from the FreeBSD mirror with wheelfs
(`~/src/public_github/wheelfs`, `cargo build --release`) and install it into
the venv in place of step 3's duckdb upgrade:

```sh
# fetch py312-duckdb-<ver>.pkg from https://pkg.freebsd.org/FreeBSD:15:amd64/latest/All/ into ~/pkgs
wheelfs materialize duckdb -o ~/wheels --pkg-dir ~/pkgs
uv pip install --python .venv/bin/python --no-index --find-links ~/wheels duckdb==<ver>
```

Once pkg catches up, upgrade through pkg and uninstall the venv copy.

## Rollback

Reinstall the previous pkg (`sudo pkg install -f py312-duckdb-<old>`, or the
`.pkg` left in `/var/cache/pkg`). If the change being rolled back came with
code that needs the new version, also check out the previous commit and rerun
step 5's `build-parsers`.
