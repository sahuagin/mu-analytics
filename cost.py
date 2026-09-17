#!/usr/bin/env python3
"""Cross-fleet cost over the telemetry sink(s), priced from config [rates].

Reads config.toml (rate table + sink paths) via stdlib tomllib, loads each
sink via stdlib sqlite3 -> polars, joins the rate table, computes per-session
cost = rate x actual tokens, and rolls up by fleet/model.

Discipline: unlisted models are FLAGGED, never silently priced. Cache-write
uses the legacy 1.25x on total (the sink drops the 5m/1h split) — see the
config note; cache-read (0.10x) dominates cache cost anyway. Fresh input is
provider-aware ([cache_in_input]): OpenAI's prompt total includes its cached
subset, so cached tokens are taken out before the input rate applies
(mu-hx0ta); the formula is the same one sample_data.priced_cost uses.

Run via the launcher so it gets the pkg python that has polars:
    ./run cost.py
"""

import os
import sqlite3
import tomllib

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = tomllib.load(open(os.path.join(HERE, "config.toml"), "rb"))
RATES = cfg["rates"]
MULT = cfg["cache_multipliers"]
PATHS = cfg["paths"]
CACHE_IN_INPUT = {k.lower(): bool(v) for k, v in cfg.get("cache_in_input", {}).items()}

COLS = (
    "SELECT provider, model, exit_reason, outcome_class, "
    "COALESCE(prompt_tokens,0)     AS input_tok, "
    "COALESCE(completion_tokens,0) AS output_tok, "
    "COALESCE(cache_read_tokens,0) AS cache_read, "
    "COALESCE(cache_write_tokens,0) AS cache_write, "
    "{cost_col} AS producer_cost "
    "FROM tasks"
)

_RATE_KEYS = sorted(RATES.keys(), key=len, reverse=True)
_PROVIDER_PREFIXES = ("anthropic/", "google/", "openai/", "openrouter/", "deepseek/", "x-ai/")


def rate_key(model):
    """Map a raw model id to a [rates] key: strip provider prefixes, then
    longest-prefix match so date-suffixed ids (…-20251001) hit the base id."""
    if not model:
        return None
    m = model.lower()
    for p in _PROVIDER_PREFIXES:
        if m.startswith(p):
            m = m[len(p) :]
            break
    for k in _RATE_KEYS:
        if m == k or m.startswith(k):
            return k
    return None


# cost_kind is config-driven and SERVING-PATH-determined (provider), not model
# identity — ollama/gemma is free, openrouter/gemma is billed. Model-level
# overrides win over the provider default; unmapped providers flag as "unknown".
_CK_PROVIDER = {k.lower(): v for k, v in cfg.get("cost_kind", {}).get("provider", {}).items()}
_CK_MODEL = {k.lower(): v for k, v in cfg.get("cost_kind", {}).get("model", {}).items()}


def cost_kind(provider, model):
    m = (model or "").lower()
    if m == "":
        return "free"
    if m in _CK_MODEL:
        return _CK_MODEL[m]
    return _CK_PROVIDER.get((provider or "").lower(), "unknown")


def load_sink(label: str, db: str):
    if not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        # the producer's per-call cost (tasks.cost_usd, mu-hx0ta) when the
        # sink has the column; older sinks price every task from totals
        has_cost = any(c[1] == "cost_usd" for c in con.execute("PRAGMA table_info(tasks)"))
        query = COLS.format(cost_col="cost_usd" if has_cost else "NULL")
        rows = [
            dict(r)
            | {
                "rate_key": rate_key(r["model"]),
                "cost_kind": cost_kind(r["provider"], r["model"]),
                "cache_in_input": CACHE_IN_INPUT.get((r["provider"] or "").lower(), False),
            }
            for r in con.execute(query)
        ]
    except sqlite3.OperationalError:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass
    if not rows:
        return None
    return pl.DataFrame(rows).with_columns(pl.lit(label).alias("fleet"))


frames = [
    f
    for f in (load_sink("mu", PATHS["mu_sink_db"]), load_sink("cc", PATHS["cc_sink_db"]))
    if f is not None
]
if not frames:
    raise SystemExit("no telemetry sink with data found (check config [paths])")
df = pl.concat(frames, how="vertical_relaxed")

# Rate table as a frame; left-join so unlisted models surface as null rates.
rates_df = pl.DataFrame(
    [{"rate_key": m, "in_rate": r["input"], "out_rate": r["output"]} for m, r in RATES.items()]
)
df = df.join(rates_df, on="rate_key", how="left")
unpriced = df.filter(pl.col("in_rate").is_null())
# Fresh input: the reported input for disjoint-bucket providers, input minus
# the cached subset where the provider counts cache inside input (mu-hx0ta).
df = df.with_columns(
    pl.when(pl.col("cache_in_input"))
    .then((pl.col("input_tok") - pl.col("cache_read") - pl.col("cache_write")).clip(lower_bound=0))
    .otherwise(pl.col("input_tok"))
    .alias("fresh_tok")
)
# The producer's per-call figure wins where the sink carries it (the only
# place a per-request pricing tier is exact); the rate card on totals is the
# base-rate fallback for tasks compacted before mu-hx0ta.
priced = df.filter(pl.col("in_rate").is_not_null()).with_columns(
    pl.coalesce(
        pl.col("producer_cost").cast(pl.Float64),
        (
            pl.col("fresh_tok") * pl.col("in_rate")
            + pl.col("cache_write") * pl.col("in_rate") * MULT["write_5m"]
            + pl.col("cache_read") * pl.col("in_rate") * MULT["read"]
            + pl.col("output_tok") * pl.col("out_rate")
        )
        / 1_000_000,
    ).alias("cost_usd")
)

print("=== cost by KIND  (billed = real $ paid; subscription = API-equiv, NOT paid; free = $0) ===")
print(
    priced.group_by("cost_kind")
    .agg(pl.len().alias("sessions"), pl.col("cost_usd").sum().round(2).alias("cost_usd"))
    .sort("cost_usd", descending=True)
)
print(f"   (+ {len(df.filter(pl.col('cost_kind') == 'free'))} free sessions at $0)\n")

print("=== cost by fleet ===")
print(
    priced.group_by("fleet")
    .agg(pl.len().alias("sessions"), pl.col("cost_usd").sum().round(2).alias("cost_usd"))
    .sort("cost_usd", descending=True)
)

print("=== cost by fleet + model ===")
print(
    priced.group_by("fleet", "model")
    .agg(pl.len().alias("sessions"), pl.col("cost_usd").sum().round(2).alias("cost_usd"))
    .sort("cost_usd", descending=True)
)

print(f"=== total: ${priced['cost_usd'].sum():,.2f} across {len(priced)} priced sessions ===")
if len(unpriced):
    print(
        f"!! {len(unpriced)} unpriced session(s) — models not in [rates]:",
        unpriced["model"].unique().to_list(),
    )

# Hand-check: recompute the most expensive session's cost by hand, compare to polars.
top = priced.sort("cost_usd", descending=True).row(0, named=True)
ir, orr = top["in_rate"], top["out_rate"]
manual = (
    top["fresh_tok"] * ir
    + top["cache_write"] * ir * MULT["write_5m"]
    + top["cache_read"] * ir * MULT["read"]
    + top["output_tok"] * orr
) / 1_000_000
print("\n=== hand-check (most expensive session) ===")
print(
    f"model={top['model']}  input={top['input_tok']} (fresh {top['fresh_tok']}, "
    f"cache_in_input={top['cache_in_input']})  output={top['output_tok']}  "
    f"cache_read={top['cache_read']}  cache_write={top['cache_write']}"
)
print(
    f"  ({top['fresh_tok']}*{ir} + {top['cache_write']}*{ir}*{MULT['write_5m']} "
    f"+ {top['cache_read']}*{ir}*{MULT['read']} + {top['output_tok']}*{orr}) / 1e6"
)
if top["producer_cost"] is not None:
    # the producer priced this task per call; the rate card on totals is the
    # base-rate figure it would have been (equal unless a request crossed a tier)
    print(
        f"  producer (per call) = ${top['cost_usd']:,.4f}   rate card on totals = ${manual:,.4f}   "
        f"tier surcharge = ${top['cost_usd'] - manual:,.4f}"
    )
else:
    print(
        f"  manual = ${manual:,.4f}   polars = ${top['cost_usd']:,.4f}   "
        f"match = {abs(manual - top['cost_usd']) < 1e-6}"
    )
