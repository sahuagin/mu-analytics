#!/usr/bin/env python3
"""Gradient-boosting probe: does objective telemetry predict operator sentiment?
(Port of claude-personal/scripts/degradation_ml.py onto the unified substrate,
refined in DS3 to a SIGNED target.)

HistGradientBoostingRegressor predicts each session's SIGNED operator-sentiment
(net = pos − neg operator-language markers, per 100 user msgs) from telemetry
features ONLY — both directions, not one-sided frustration. Four readouts:
  1. cross-validated R2/MAE — is there an objective signature of good/bad sessions?
  2. permutation importances — which telemetry axes carry it.
  3. out-of-fold residual extremes (both tails): telemetry rosier than the operator
     felt = candidate UNNOTICED-degraded; operator warmer than telemetry predicts.
  4. the UNATTENDED fleet (no operator language) scored by the trained model —
     telemetry is the only witness; most net-negative first to autopsy.

Substrate change vs the legacy: features come from features.session_features
(the ev view, DuckDB — no sqlite sinks, no mu_stats.sql) and the signed label
from scans.scan_sentiment (operator-only, meta-filtered). session_ref is the
canonical fleet:session key, so features and label join directly.

Deterministic (fixed seed). Output: degradation-ml.{md,json} — same contract
gen_degradation_page.py consumes.

Run:  ./run degradation.py [out.md]
"""

import datetime
import json
import sys
from pathlib import Path

import engine
import features
import scans

# numpy + scikit-learn are the `ml` optional extra (pyproject.toml) — imported
# lazily inside train()/render(), the only paths that touch them. assemble() is
# pure Python + DuckDB, so the substrate-join unit test runs in the lean CI gate
# without the heavy ML deps (mirrors cost.py/polars being an optional diagnostic).
RANDOM_STATE = 42
TOPN = 12


def _label(con):
    """Per-session frustration rate from the ported scan (operator language only,
    meta-filtered). Keyed by the canonical session_ref — joins features directly.

    DS3: SIGNED sentiment — net = pos - neg operator-language markers — replaces the
    one-sided frustration count, so the probe predicts a signed deviation (both
    'went well' and 'unnoticed-degraded')."""
    _hits, all_rows, _totals = scans.scan_sentiment(con)
    return {
        ref: {
            "window": win,
            "first_ts": fts,
            "n_user": n_user,
            "pos": pos,
            "neg": neg,
            "net": net,
            "ending": ending,
        }
        for ref, win, fts, n_user, pos, neg, net, ending in all_rows
    }


def assemble(con):
    """Join the telemetry features to the signed-sentiment label and build X/y plus
    the unattended (label-less) set. Returns a dict — testable without training."""
    feat = features.session_features(con)
    lang = _label(con)
    rows_j = [r for r in feat if r["session_ref"] in lang]
    unattended = [r for r in feat if r["session_ref"] not in lang]

    providers = sorted({r["provider"] for r in rows_j if r["provider"]})
    models = sorted({r["model"] for r in rows_j if r["model"]})
    names = (
        list(features.NUMERIC)
        + [f"provider={p}" for p in providers]
        + [f"model={m}" for m in models]
        + ["harness=mu"]
    )

    def vec(r):
        return (
            [float(r.get(n) or 0) for n in features.NUMERIC]
            + [1.0 * (r["provider"] == p) for p in providers]
            + [1.0 * (r["model"] == m) for m in models]
            + [1.0 * (r["fleet"] == "mu")]
        )

    # Plain Python lists — numpy-free so the substrate-join test runs in the lean
    # CI gate; train()/render() asarray() these when the ml extra is present.
    X = [vec(r) for r in rows_j]
    # SIGNED target (DS3): net = pos - neg markers per 100 user msgs. Negative =
    # net-frustrated session, positive = net-praised; the probe predicts the sign.
    y = [
        100.0 * lang[r["session_ref"]]["net"] / max(lang[r["session_ref"]]["n_user"], 1)
        for r in rows_j
    ]
    X_un = [vec(r) for r in unattended]
    return {
        "feat": feat,
        "lang": lang,
        "rows_j": rows_j,
        "unattended": unattended,
        "names": names,
        "X": X,
        "y": y,
        "X_un": X_un,
    }


def train(X, y, n_splits=5):
    """5-fold out-of-fold predictions + a fit model + permutation importances.
    numpy/sklearn imported here (the `ml` extra) — assemble() stays dep-free."""
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.inspection import permutation_importance
    from sklearn.model_selection import KFold, cross_val_predict

    X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
    cv = KFold(n_splits=min(n_splits, len(y)), shuffle=True, random_state=RANDOM_STATE)
    gb = HistGradientBoostingRegressor(random_state=RANDOM_STATE)
    y_oof = cross_val_predict(gb, X, y, cv=cv)
    gb.fit(X, y)
    imp = permutation_importance(gb, X, y, n_repeats=10, random_state=RANDOM_STATE)
    return y_oof, gb, imp


def _window(started, _et=scans.ET):
    """ET weekend/weekday label for unattended sessions (started_at ISO string)."""
    try:
        dt = datetime.datetime.fromisoformat(started)
    except (ValueError, TypeError):
        return "unknown-ts"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    loc = dt.astimezone(_et)
    return scans.weekend_label(loc) or scans.weekday_label(loc)


def render(a, y_oof, gb, imp, out: Path):
    import numpy as np
    from sklearn.metrics import mean_absolute_error, r2_score

    rows_j, lang, names = a["rows_j"], a["lang"], a["names"]
    y = np.asarray(a["y"], dtype=float)
    r2, mae = r2_score(y, y_oof), mean_absolute_error(y, y_oof)
    ranked = sorted(
        zip(names, imp.importances_mean, imp.importances_std, strict=True), key=lambda t: -t[1]
    )
    # resid = pred - obs. Signed target: resid > 0 means telemetry predicts a MORE
    # POSITIVE sentiment than the operator expressed (telemetry looks fine, operator
    # was unhappier) — the candidate UNNOTICED-degraded direction.
    resid = y_oof - y
    order = np.argsort(resid)

    def _row(i):
        r, lg = rows_j[i], lang[rows_j[i]["session_ref"]]
        return (
            f"| {r['session_ref'][:44]} | {lg['window']} | {lg['first_ts'][:16]} | {y[i]:.1f} "
            f"| {y_oof[i]:.1f} | {lg['n_user']} | {int(r['tool_calls'])} | {r['cost_usd']:.2f} "
            f"| {lg['ending']} |"
        )

    hdr = (
        "| session | window | started | obs | pred | msgs | tools | $ | ending |\n"
        "|---|---|---|---|---|---|---|---|---|"
    )
    md = [
        "# degradation-ml — telemetry → operator-sentiment probe (signed)",
        "",
        f"sessions joined: {len(rows_j)} (telemetry {len(a['feat'])}, scan {len(lang)}) · "
        f"target: SIGNED sentiment net (pos−neg)/100msg · model: HistGradientBoosting, 5-fold OOF",
        "",
        "## 1. Predictive skill (objective telemetry only)",
        f"out-of-fold R2 = {r2:.3f} · MAE = {mae:.2f} net/100msg "
        f"(target mean {y.mean():.2f}, sd {y.std():.2f}; negative = net-frustrated)",
        "",
        "## 2. What carries the signal (permutation importance)",
        "| feature | importance | sd |",
        "|---|---|---|",
        *[f"| {n} | {m:.3f} | {s:.3f} |" for n, m, s in ranked[:TOPN]],
        "",
        "## 3a. Telemetry rosier than the operator felt (candidate UNNOTICED-degraded)",
        hdr,
        *[_row(i) for i in order[::-1][:TOPN]],
        "",
        "## 3b. Operator warmer than telemetry predicts",
        hdr,
        *[_row(i) for i in order[:TOPN]],
    ]

    un = a["unattended"]
    score = gb.predict(np.asarray(a["X_un"], dtype=float)) if len(un) else np.array([])
    if len(un):
        rank = np.argsort(score)  # most net-negative predicted sentiment first
        md += [
            "",
            f"## 4. Unattended fleet ({len(un)} sessions, no operator language) "
            "ranked by predicted sentiment (most net-negative first = candidate degraded)",
            "proxy score: signed net the model expects from this telemetry; ranking, not a verdict",
            "| session | window | started | score | calls | tools | $ |",
            "|---|---|---|---|---|---|---|",
        ]
        for i in rank[:TOPN]:
            r = un[i]
            md.append(
                f"| {r['session_ref'][:44]} | {_window(r['started_at'])} | {r['started_at'][:16]} "
                f"| {score[i]:.0f} | {r['calls']} | {int(r['tool_calls'])} | {r['cost_usd']:.2f} |"
            )

    md += [
        "",
        "scope: cc sessions carry 0 for mu-only telemetry (wall/gaps) — fleet captured via "
        "harness/provider/model one-hots. Features: ev view (DuckDB); label: scans.scan_sentiment.",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md))

    # machine-readable artifact (gen_degradation_page.py contract)
    recs = [
        {
            "session_ref": rows_j[i]["session_ref"],
            "kind": "interactive",
            "started": lang[rows_j[i]["session_ref"]]["first_ts"] or rows_j[i]["started_at"],
            "window": lang[rows_j[i]["session_ref"]]["window"],
            "n_user": lang[rows_j[i]["session_ref"]]["n_user"],
            "pos": lang[rows_j[i]["session_ref"]]["pos"],
            "neg": lang[rows_j[i]["session_ref"]]["neg"],
            "net": lang[rows_j[i]["session_ref"]]["net"],
            "obs": round(float(y[i]), 1),
            "pred": round(float(y_oof[i]), 1),
            "ending": lang[rows_j[i]["session_ref"]]["ending"],
            "calls": int(rows_j[i]["calls"]),
            "tool_calls": int(rows_j[i]["tool_calls"]),
            "cost": float(rows_j[i]["cost_usd"]),
        }
        for i in range(len(rows_j))
    ]
    for i in range(len(un)):
        r = un[i]
        recs.append(
            {
                "session_ref": r["session_ref"],
                "kind": "unattended",
                "started": r["started_at"],
                "window": _window(r["started_at"]),
                "pred": round(float(score[i]), 1),
                "calls": int(r["calls"]),
                "tool_calls": int(r["tool_calls"]),
                "cost": float(r["cost_usd"]),
            }
        )
    out.with_suffix(".json").write_text(
        json.dumps(
            {
                "meta": {
                    "r2": round(r2, 3),
                    "mae": round(mae, 2),
                    "n_interactive": len(rows_j),
                    "n_unattended": len(un),
                    "importances": [[n, round(m, 3)] for n, m, _ in ranked[:TOPN]],
                },
                "sessions": recs,
            }
        )
    )
    return r2, mae, ranked


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "mu-stats/degradation-ml.md"
    a = assemble(engine.connect())
    print(
        f"coverage: telemetry={len(a['feat'])} scan-qualifying={len(a['lang'])} "
        f"joined={len(a['rows_j'])} unattended={len(a['unattended'])}"
    )
    if len(a["y"]) < 5:
        print("too few joined sessions to train (need >=5)")
        return
    y_oof, gb, imp = train(a["X"], a["y"])
    r2, mae, ranked = render(a, y_oof, gb, imp, out)
    print(f"R2={r2:.3f} MAE={mae:.2f} -> {out} (+ .json)")
    print("top features:", ", ".join(f"{n}={m:.3f}" for n, m, _ in ranked[:6]))


if __name__ == "__main__":
    main()
