#!/usr/bin/env python3
"""Anomaly-ranked marking worklist (DS1 port onto the unified substrate).

Aims scarce operator marks at statistically *unusual* sessions. Builds the
per-session feature table from features.session_features (the `ev` view, DuckDB —
no mu_stats.sql, no sqlite sinks), scores every session with an IsolationForest,
and emits a markdown worklist ranked by anomaly score: the top UNMARKED sessions,
each annotated with the features that make it weird (per-feature z-scores), so the
operator can mark from the top down and break the labeling bottleneck.

Substrate change vs the legacy:
  - features come from features.session_features (shared backbone with
    degradation), not the harness_costs/ask_telemetry/tool_mix sqlite views;
  - marks come from marks_store.read_marks_by_session — operator_mark events in
    the ev view (DuckDB-native) unioned with the dashboard's mark buffer — keyed
    by the canonical session_ref, so features and marks join directly.

Two legacy inputs are intentionally NOT carried (they live outside the event
substrate, so honoring the DuckDB-only end-state means dropping them here):
  - the session_incidents section (task_log-sourced) — if incidents should drive
    the worklist, they need to become events first (flag for the event stream);
  - the cache-write 5m/1h tier split (cw_5m_tok/cw_1h_tok, from session_costs) —
    the unified task_telemetry carries total cache_write_tok only. Candidate
    enrichment if the TTL split proves load-bearing.

────────────────────────────────────────────────────────────────────────────
⚠ EXPLORATORY, NOT INFERENTIAL — "anomaly" means statistically UNUSUAL, not bad.
  The rank says "look here," never "this failed." Confounds (cost ~ length ~
  difficulty ~ time-of-day) move together; the named z-scores show WHICH features
  drove a score, not WHY they co-occur. Several features are mu-only (wall p50/p95,
  gaps, tool calls): cc sessions carry 0 there and surface via the provider/model
  one-hots instead. Findings graduate to a pre-registered test or stay curiosities.
────────────────────────────────────────────────────────────────────────────

numpy/scikit-learn are the `ml` optional extra: build_table() is dep-free (the
feature+marks join is the load-bearing, unit-tested part); only score()/render()
import the ML libs (lazily). Deterministic: fixed seed, single-threaded fit,
stable sort — same data yields the same list every run.

Run:  ./run anomaly_worklist.py [out.md]   (default ~/mu-stats/anomaly-worklist.md)
"""

import sys
from pathlib import Path

import engine
import features
import marks_store

RANDOM_STATE = 42  # determinism: fixed seed for the forest
WORKLIST_N = 30
RARE_FRAC = 0.10  # provider/model held by <10% of sessions is "rare"

# (key, display label, value format) — the numeric block fed to the model. Keys
# are features.NUMERIC; the standardized values ARE the per-feature z-scores used
# to name why a session is weird.
NUM_FEATURES = [
    ("calls", "asks", "{:,}"),
    ("input_tok", "input tok", "{:,}"),
    ("output_tok", "output tok", "{:,}"),
    ("cache_read_tok", "cache-read tok", "{:,}"),
    ("cache_write_tok", "cache-write tok", "{:,}"),
    ("cost_usd", "cost", "${:,.2f}"),
    ("wall_p50", "wall p50 (ms)", "{:,.0f}"),
    ("wall_p95", "wall p95 (ms)", "{:,.0f}"),
    ("gaps_over_5m", "gaps >5m", "{:,}"),
    ("tool_calls", "tool calls", "{:,}"),
    ("n_tasks", "tasks", "{:,}"),
    ("hour_of_day", "hour-of-day", "{:.0f}"),
    ("day_of_week", "day-of-week", "{:.0f}"),
]
# Heavy-tailed counts get log1p before standardizing so no single scale dominates;
# hour/day-of-week stay linear (small bounded range).
LOG_FEATURES = {
    "calls",
    "input_tok",
    "output_tok",
    "cache_read_tok",
    "cache_write_tok",
    "cost_usd",
    "wall_p50",
    "wall_p95",
    "gaps_over_5m",
    "tool_calls",
    "n_tasks",
}
NUM_NAMES = [n for n, _, _ in NUM_FEATURES]


def build_table(con):
    """Per-session feature rows (features.session_features) annotated with the
    latest operator rating, plus the unmarked/marked partition. numpy-free so the
    join + marks-keying is unit-testable in the lean CI gate."""
    feat = features.session_features(con)
    marks = marks_store.read_marks_by_session(con)
    for r in feat:
        m = marks.get(r["session_ref"])
        r["rating"] = m["rating"] if m else None
        r["note"] = m["note"] if m else ""
    unmarked = [r for r in feat if r["rating"] is None]
    marked = [r for r in feat if r["rating"] is not None]
    return {"feat": feat, "marks": marks, "unmarked": unmarked, "marked": marked}


def score(feat):
    """Standardize (log1p the heavy tails) + one-hot provider/model, fit a
    deterministic IsolationForest, and annotate each row with an anomaly_score
    (higher = weirder). Returns the artifacts why_weird() needs. Lazy ML imports
    (the `ml` extra) — build_table() stays dep-free."""
    import numpy as np
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler

    raw = np.array([[float(r[n] or 0) for n in NUM_NAMES] for r in feat], dtype=float)
    trans = raw.copy()
    for j, n in enumerate(NUM_NAMES):
        if n in LOG_FEATURES:
            trans[:, j] = np.log1p(np.clip(trans[:, j], 0, None))
    z_num = StandardScaler().fit_transform(trans)  # == per-feature z-scores

    def onehot(key):
        cats = sorted({r[key] or "" for r in feat})
        idx = {c: i for i, c in enumerate(cats)}
        m = np.zeros((len(feat), len(cats)), dtype=float)
        for i, r in enumerate(feat):
            m[i, idx[r[key] or ""]] = 1.0
        freq = {c: sum(1 for r in feat if (r[key] or "") == c) for c in cats}
        return m, freq

    prov_oh, prov_freq = onehot("provider")
    model_oh, model_freq = onehot("model")
    X = np.hstack([z_num, prov_oh, model_oh])

    forest = IsolationForest(
        n_estimators=256, contamination="auto", random_state=RANDOM_STATE, n_jobs=1
    )
    forest.fit(X)
    # decision_function: lower = more anomalous → negate so higher = weirder.
    anomaly = -forest.decision_function(X)
    for i, r in enumerate(feat):
        r["anomaly_score"] = float(anomaly[i])
    return {"z_num": z_num, "prov_freq": prov_freq, "model_freq": model_freq}


def why_weird(r, feat, z_num, prov_freq, model_freq, k=3):
    """Name the k most-deviant numeric features (by |z|) plus any rare categorical
    membership — the inspectable 'why' behind the score."""
    i = feat.index(r)
    z = z_num[i]
    order = sorted(range(len(NUM_NAMES)), key=lambda j: -abs(z[j]))
    parts = []
    for j in order[:k]:
        if abs(z[j]) < 0.5:  # nothing notable left
            break
        _name, label, fmt = NUM_FEATURES[j]
        parts.append(f"{label}={fmt.format(r[NUM_NAMES[j]] or 0)} (z={z[j]:+.1f})")
    n_total = len(feat)
    prov, model = r["provider"] or "", r["model"] or ""
    if prov and prov_freq.get(prov, 0) / n_total < RARE_FRAC:
        parts.append(f"rare provider: {prov} (n={prov_freq[prov]})")
    if model and model_freq.get(model, 0) / n_total < RARE_FRAC:
        parts.append(f"rare model: {model} (n={model_freq[model]})")
    return "; ".join(parts) if parts else "(broadly typical; ranked by joint structure)"


def _short(ref, n=42):
    return ref if len(ref) <= n else ref[: n - 1] + "…"


_CAUTION = f"""\
> ⚠ **EXPLORATORY, NOT INFERENTIAL.** "Anomaly" = statistically *unusual*, **not
> bad**. A weird session may be a long legitimate task, an unusually cheap one, or
> one run at an odd hour. This list says *"look here"*, never *"this failed."*
>
> - **Confounds are entangled**: cost ~ length ~ difficulty ~ time-of-day move
>   together. A high score usually reflects several at once; the named z-scores
>   show *which* features drove it, not *why* they co-occur.
> - **Some features are mu-only** (wall p50/p95, gaps, tool calls): claude-code
>   sessions carry 0 there and surface via the provider/model one-hots instead.
> - **Findings graduate or stay curiosities.** This is a labeling accelerator
>   (active-learning toward supervised viability), not evidence of a population
>   effect.
> - **Deterministic**: fixed `random_state={RANDOM_STATE}`, single-threaded fit,
>   stable sort — same data → same list."""


def render(table, art, out: Path):
    feat, marked = table["feat"], table["marked"]
    z_num, prov_freq, model_freq = art["z_num"], art["prov_freq"], art["model_freq"]

    ranked = sorted(feat, key=lambda r: (-r["anomaly_score"], r["session_ref"]))
    rank_of = {r["session_ref"]: i + 1 for i, r in enumerate(ranked)}
    worklist = [r for r in ranked if r["rating"] is None][:WORKLIST_N]
    marked_ranked = [r for r in ranked if r["rating"] is not None]

    n_total = len(feat)
    n_mu = sum(1 for r in feat if r["fleet"] == "mu")
    n_cc = n_total - n_mu

    def table_md(items, include_rating=False):
        cols = "| # | score | fleet | session_ref | model | cost | started |"
        sep = "|---|------:|-------|-------------|-------|-----:|---------|"
        if include_rating:
            cols += " marked |"
            sep += "--------|"
        cols += " why weird (named z-scores) |"
        sep += "----------------------------|"
        lines = [cols, sep]
        for r in items:
            cells = [
                str(rank_of[r["session_ref"]]),
                f"{r['anomaly_score']:.3f}",
                r["fleet"],
                f"`{_short(r['session_ref'])}`",
                r["model"] or "?",
                f"${r['cost_usd']:.2f}",
                (r["started_at"] or "")[:16],
            ]
            if include_rating:
                cells.append(f"{r['rating']}/5" if r["rating"] is not None else "—")
            cells.append(why_weird(r, feat, z_num, prov_freq, model_freq))
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    md = f"""# Anomaly-ranked marking worklist

_Generated by `anomaly_worklist.py` (DS1 · unified substrate) · IsolationForest
(n_estimators=256, random_state={RANDOM_STATE}) over {n_total} sessions
({n_mu} mu + {n_cc} claude-code) · features: features.session_features (ev view);
marks: marks_store.read_marks_by_session ({len(marked)} marked)._

{_CAUTION}

**Purpose** — aim scarce operator marks at statistically weird sessions to break
the {len(marked)}/{n_total} labeling bottleneck. Mark from the top down; each row
names the features that make it unusual so you know what you're looking at before
you open it.

## Worklist — top {len(worklist)} weirdest UNMARKED sessions

Mark these first. Score is the IsolationForest anomaly score (higher = weirder);
`#` is the rank across all {n_total} sessions.

{table_md(worklist)}

## Already-marked sessions (where they rank)

A sanity check: marked sessions are *labeled*, not necessarily *weird* — they
should not dominate the top of the ranking.

{table_md(marked_ranked, include_rating=True) if marked_ranked else "_No marked sessions yet._"}
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    return rank_of, worklist


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "mu-stats/anomaly-worklist.md"
    table = build_table(engine.connect())
    feat = table["feat"]
    if not feat:
        print("no priced sessions in the ev view — nothing to rank")
        return
    art = score(feat)
    rank_of, worklist = render(table, art, out)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    print(
        f"  {len(feat)} sessions, {len(table['marked'])} marked, {len(table['unmarked'])} unmarked"
    )
    print("\nTop 10 weirdest UNMARKED sessions (mark from the top):\n")
    for r in worklist[:10]:
        print(
            f"  #{rank_of[r['session_ref']]:<3} score={r['anomaly_score']:.3f}  "
            f"{r['fleet']}  {_short(r['session_ref'], 40):<40}  ${r['cost_usd']:.2f}"
        )
        print(
            f"        why: {why_weird(r, feat, art['z_num'], art['prov_freq'], art['model_freq'])}"
        )


if __name__ == "__main__":
    main()
