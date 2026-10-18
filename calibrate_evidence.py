#!/usr/bin/env python3
"""Compare transcript-only vs recorded-tool evidence on fixed synthetic labels.

No corpus discovery or deployment. Model target is role-resolved and provider
pinned; output files preserve prompts, expected labels, and raw verdicts.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

import duckdb

import pointwise_judge as pw
from judge_evidence import collect_evidence, render_evidence

CASES = Path(__file__).parent / "behavior-judge/calibration/structured-evidence-cases.json"


def case_inputs(case):
    con = duckdb.connect()
    try:
        con.execute(
            "CREATE TABLE ev (id BIGINT, ts BIGINT, kind VARCHAR, payload JSON, fleet VARCHAR, session VARCHAR, filename VARCHAR)"
        )
        turns = []

        def add(event_id, kind, payload, body):
            con.execute(
                "INSERT INTO ev VALUES (?, ?, ?, ?, 'fixture', 'session', 'synthetic')",
                [event_id, event_id * 100, kind, json.dumps(payload)],
            )
            turns.append(
                pw.Turn(
                    event_id,
                    len(turns) + 1,
                    kind,
                    payload.get("name"),
                    payload.get("is_error"),
                    body,
                )
            )

        if case["command"] is not None:
            add(
                1,
                "tool_call",
                {"call_id": "call", "name": "shell", "arguments": {"command": case["command"]}},
                case["command"],
            )
        if case["result"] is not None:
            add(
                2,
                "tool_result",
                {"call_id": "call", "content": case["result"], "is_error": case["is_error"]},
                case["result"],
            )
        add(10, "assistant_message_event", {}, case["claim"])
        text = (
            "Synthetic calibration sequence. Assess only the final assistant claim.\n\n"
            + "\n\n".join(pw._render_turn(t, max(1, len(t.body))) for t in turns)
        )
        packet = collect_evidence(con, "fixture", "session", 10)
        return {
            "transcript_only": text,
            "structured_evidence": render_evidence(packet) + "\n\n" + text,
        }
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--role", default="judge")
    ap.add_argument("--require-provider", required=True)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    os.umask(0o077)
    args.out.mkdir(parents=True, exist_ok=True)
    target = pw.resolve_target(args.role, args.require_provider)
    cases = json.loads(CASES.read_text())
    if args.limit is not None:
        cases = cases[: args.limit]
    report = []
    for case in cases:
        for mode, text in case_inputs(case).items():
            digest = hashlib.sha256(text.encode()).hexdigest()
            path = args.out / f"{case['id']}-{mode}.json"
            version = pw.analyzer_version(case["class"])
            if path.exists():
                result = json.loads(path.read_text())
                if (result["input_hash"], result["target"], result["analyzer_version"]) != (
                    digest,
                    target,
                    version,
                ):
                    raise ValueError(
                        "existing results use different input, target, or rubric; choose a fresh output directory"
                    )
            else:
                (args.out / f"{case['id']}-{mode}.txt").write_text(text)
                result = {
                    "case": case["id"],
                    "mode": mode,
                    "expected": case["expected"],
                    "label_reason": case["reason"],
                    "input_hash": digest,
                    "target": target,
                    "analyzer_version": version,
                }
                try:
                    v = pw.judge_chunk(
                        pw.Chunk(0, 1, 10, text, digest),
                        case["class"],
                        args.role,
                        args.require_provider,
                        args.timeout,
                    )
                    claimed, verified = pw.evidence_counts(v)
                    result.update(
                        status="ok",
                        verdict=v,
                        classification_correct=v["occurred"] == case["expected"],
                        evidence_claimed=claimed,
                        evidence_verified=verified,
                        all_evidence_verified=claimed > 0 and claimed == verified,
                    )
                except Exception as exc:
                    result.update(
                        status="error", error_type=type(exc).__name__, diagnostic=str(exc)
                    )
                path.write_text(json.dumps(result, indent=2))
            report.append(result)
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in result.items()
                        if k not in ("verdict", "diagnostic", "label_reason")
                    }
                ),
                flush=True,
            )
    summary = {}
    for mode in ("transcript_only", "structured_evidence"):
        rows = [r for r in report if r["mode"] == mode]
        summary[mode] = {
            "attempts": len(rows),
            "valid": sum(r["status"] == "ok" for r in rows),
            "correct": sum(r.get("classification_correct", False) for r in rows),
            "fully_verified": sum(r.get("all_evidence_verified", False) for r in rows),
        }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
