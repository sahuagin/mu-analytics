#!/usr/bin/env python3
"""Generate the live dashboard: inject verified sink data into index.html.

index.html is the design template with a `const DATA = /*BEGIN_DATA*/…/*END_DATA*/`
placeholder. This script replaces what's between the markers with the real
object from sample_data.build(), then writes a self-contained dist/ (the filled
index.html + a copy of assets/). Point nginx at dist/.

Drop into cron for a self-updating page, e.g.:
    @hourly  /path/to/mu-analytics/run gen_dashboard.py

Run:  ./run gen_dashboard.py  [out_html_path]
"""

import json
import os
import re
import shutil
import sys

# MU_ANALYTICS_DEMO=1 swaps real sink data for the fabricated demo set
# (used to render the README screenshot without exposing real usage).
if os.environ.get("MU_ANALYTICS_DEMO"):
    from demo_data import build
else:
    from sample_data import build

HERE = os.path.dirname(os.path.abspath(__file__))


def _preflight_paths(paths) -> None:
    """Warn loudly to stderr when configured INPUT paths don't exist.

    The silent-empty trap: a fresh `cp config.example.toml config.toml` leaves the
    `[paths]` at their `/home/USER/...` placeholders. `_load()` skips a db that
    doesn't exist (no error), so `build()` returns an empty contract and this page
    renders a zeroed shell with NO indication why. A loud warning here turns
    "dashboard opens but shows no data" into an actionable message.
    """
    missing = []
    for key in ("mu_sink_db", "cc_sink_db"):  # cc_events_out/dashboard_out are outputs
        p = paths.get(key)
        if isinstance(p, str) and not os.path.exists(os.path.expanduser(p)):
            missing.append(f"{key} = {p}")
    roots = paths.get("cc_log_roots")
    if isinstance(roots, list):
        for root in roots:
            if isinstance(root, str) and not os.path.exists(os.path.expanduser(root)):
                missing.append(f"cc_log_roots = {root}")
    if not missing:
        return
    print(
        "WARNING: config.toml [paths] point at files that do not exist — the "
        "dashboard will render EMPTY.\n"
        "         Edit [paths] for this machine "
        "(config.example.toml ships /home/USER/... placeholders).",
        file=sys.stderr,
    )
    for m in missing:
        print(f"         missing: {m}", file=sys.stderr)


# The proto is the live template (it superseded the old single-page index.html).
# It references ../assets (it lives in proto/); the output dir gets its own assets/,
# so normalize the paths on the way out.
template = open(os.path.join(HERE, "proto", "index.html"), encoding="utf-8").read()
template = template.replace("../assets/", "assets/")
if not os.environ.get("MU_ANALYTICS_DEMO"):
    from sample_data import PATHS

    _preflight_paths(PATHS)
data = build()
payload = json.dumps(data, indent=2)

# Function-replacement so the JSON is inserted literally (no regex backref games).
filled, n = re.subn(
    r"/\*BEGIN_DATA\*/.*?/\*END_DATA\*/",
    lambda _m: "/*BEGIN_DATA*/" + payload + "/*END_DATA*/",
    template,
    count=1,
    flags=re.DOTALL,
)
if n != 1:
    sys.exit(f"ERROR: /*BEGIN_DATA*/…/*END_DATA*/ markers not found in proto/index.html (n={n})")

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "dist", "index.html")
outdir = os.path.dirname(out)
os.makedirs(outdir, exist_ok=True)

# The page references assets/ relatively; make sure they sit next to the output.
assets_dst = os.path.join(outdir, "assets")
if not os.path.isdir(assets_dst):
    shutil.copytree(os.path.join(HERE, "assets"), assets_dst)

with open(out, "w", encoding="utf-8") as f:
    f.write(filled)

print(f"wrote {out}  ({len(filled):,} bytes)")
print(
    f"  as_of={data['as_of']}  total=${data['kpi']['total_api_rate_equiv']:,.2f}  "
    f"billed=${data['kpi']['by_kind'].get('billed', 0):,.2f}  "
    f"sessions={sum(x['sessions'] for x in data['cost_by_kind'])}"
)

# Per-session transcript sidecars (sessions/<slug>.json). The drill-down fetches these
# on demand, so the page stays light instead of embedding ~450 MB of conversation.
# FULL fidelity — every turn, no clipping — because this is the review-and-mark surface.
import panels  # noqa: E402

sessions_dir = os.path.join(outdir, "sessions")
if os.environ.get("MU_ANALYTICS_DEMO"):
    import demo_data  # noqa: E402

    os.makedirs(sessions_dir, exist_ok=True)
    demo_tx = demo_data.demo_transcripts()
    for sid, turns in demo_tx.items():
        with open(
            os.path.join(sessions_dir, panels._slug(sid) + ".json"), "w", encoding="utf-8"
        ) as f:
            json.dump(turns, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  transcripts: wrote {len(demo_tx)} demo session sidecars -> {sessions_dir}/")
else:
    import engine  # noqa: E402

    if engine.events_present():
        stats = panels.write_session_transcripts(engine.connect(), sessions_dir)
        print(
            f"  transcripts: wrote {stats['written']} changed / {stats['total']} session "
            f"sidecars -> {sessions_dir}/"
        )
    else:
        print("  transcripts: no event log present; drill-down will show the empty state")
