#!/usr/bin/env python3
"""Per-turn context-size disparity over the TYPED, unified `ev` view.

Supersedes the round-1 hand-parse prototype (scripts/adherence_probe.py), which
read raw jsonl directly and so (a) read the superseded raw cc transcripts instead
of the cc_telemetry-converted SessionEvent stream and (b) broke on schema. Here
both fleets come from engine.py's `ev` view (cc via cc_telemetry/mu_anthropic_py,
mu native) — one schema, no hand-parse. Run on the deployed host (threadripper):

    cd ~/src/public_github/mu-analytics && python3 scripts/context_disparity.py

Per-turn context:
  cc = assistant_message_event.message.usage  (input + cache_read + cache_creation)
  mu = context_assembly.token_count_estimate
Per session: initial = first turn with ctx>0, max = peak, growth = max/initial.
"""
import os
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engine  # noqa: E402

# cc usage key spelling confirmed against live data: input_tokens,
# cache_read_input_tokens, cache_creation_input_tokens (the *_1h field is a
# sub-breakdown of cache_creation, NOT additive — do not sum it). COALESCE keeps
# this robust if cc_telemetry ever normalizes to the task_telemetry spelling.
CC_CTX = (
    "COALESCE(json_extract(payload,'$.message.usage.input_tokens')::BIGINT,"
    " json_extract(payload,'$.message.usage.prompt_tokens')::BIGINT,0)"
    "+COALESCE(json_extract(payload,'$.message.usage.cache_read_input_tokens')::BIGINT,"
    " json_extract(payload,'$.message.usage.cache_read_tokens')::BIGINT,0)"
    "+COALESCE(json_extract(payload,'$.message.usage.cache_creation_input_tokens')::BIGINT,"
    " json_extract(payload,'$.message.usage.cache_write_tokens')::BIGINT,0)"
)
MU_CTX = "json_extract(payload,'$.token_count_estimate')::BIGINT"

# mu's FauxProvider (crates/mu-ai/src/faux.rs) test sessions — the mu analog of
# cc bench. In the deployed data they surface as model='faux' (824 sessions, all
# stamped provider_kind='anthropic_api', so they inflate that bucket ~7x). They
# emit NO context_assembly, so this exclusion is a NO-OP for the disparity metric
# (verified round 8) — kept for correctness and reused when graduating
# task_telemetry-derived features (token/cost/provider mix), where faux DOES skew.
FAUX_MU = (
    "session NOT IN (SELECT DISTINCT session FROM ev WHERE fleet='mu' "
    "AND kind='task_telemetry' AND (json_extract_string(payload,'$.model')='faux' "
    "OR json_extract_string(payload,'$.provider_kind') IN ('faux','mock')))"
)


def per_session(con, fleet, kind, ctx_expr, extra=""):
    """(initial, max) per session, keeping only ctx>0 turns; >=2 turns required.
    `extra` is an optional extra WHERE predicate (e.g. the faux exclusion)."""
    where = f"fleet='{fleet}' AND kind='{kind}'" + (f" AND {extra}" if extra else "")
    rows = con.execute(
        f"WITH t AS (SELECT session, {ctx_expr} AS ctx, id FROM ev WHERE {where}) "
        "SELECT arg_min(ctx,id) AS initial, max(ctx) AS maxc "
        "FROM t WHERE ctx>0 GROUP BY session HAVING count(*)>=2"
    ).fetchall()
    return [(int(a), int(b)) for a, b in rows if a and b]


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(p * len(xs))))] if xs else 0


def report(name, rows):
    if not rows:
        print(f"\n[{name}] no sessions")
        return
    init = [r[0] for r in rows]
    mx = [r[1] for r in rows]
    print(f"\n[{name}] sessions(>=2 ctx turns): {len(rows):,}")
    print(f"  initial ctx : med={int(st.median(init)):>8,}  p90={q(init,.9):>9,}")
    print(f"  max ctx     : med={int(st.median(mx)):>8,}  p90={q(mx,.9):>9,}  max={max(mx):>10,}")
    print(f"  growth x    : med={st.median([m / i for i, m in rows]):.2f}")


def main():
    con = engine.connect()
    report("cc (typed ev)", per_session(con, "cc", "assistant_message_event", CC_CTX))
    report("mu (typed ev, ex-faux)", per_session(con, "mu", "context_assembly", MU_CTX, FAUX_MU))


if __name__ == "__main__":
    main()
