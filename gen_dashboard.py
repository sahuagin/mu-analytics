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

# The proto is the live template (it superseded the old single-page index.html).
# It references ../assets (it lives in proto/); the output dir gets its own assets/,
# so normalize the paths on the way out.
template = open(os.path.join(HERE, "proto", "index.html"), encoding="utf-8").read()
template = template.replace("../assets/", "assets/")
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
