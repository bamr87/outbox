"""Claim desk: race two verifiers on a draft's factual claims before it merges.

Each claim becomes a ``source_claim`` task for the existing arena. The scout
replays what the claim's sources said over time as env events; claimant-a
answers fast from its first read; claimant-b answers late but current. At close
the arena's winner rule picks the answer the sources support now, and the desk
passes a claim only when that winner agrees with the draft.

Ground truth is not known at close, so a desk task opens with
``expected_answer`` unset and is re-scored from its own ledger once a resolver
learns the truth. That re-score is what calibration (Brier) is computed from.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .arena import Arena
from .mailbox import write_json
from .score import answers_match, load_jsonl, score_trial

FAMILY = "source_claim"
MIN_CONFIDENCE = 0.6


def build_task(
    claim: dict[str, Any],
    timeline: list[dict[str, Any]],
    *,
    closes_at_tick: int = 8,
    lag_b: int = 2,
) -> dict[str, Any]:
    """Turn one claim and its sources' observation timeline into an arena task.

    ``timeline`` rows are ``{tick, source, says, fact, note}``: what a source
    said when the scout re-read it. Rows after the close are invisible to the
    desk; they only matter to a later recheck or resolution.
    """
    sources = set(claim.get("sources") or [])
    visible = sorted(
        (row for row in timeline if int(row["tick"]) <= closes_at_tick and (not sources or row["source"] in sources)),
        key=lambda row: int(row["tick"]),
    )
    schedule = [
        {
            "tick": int(row["tick"]),
            "env_seq": seq,
            "fact": row.get("fact") or f"{row['source']} says {row['says']}",
            "answer_hint": row["says"],
            "note": row.get("note", ""),
            "source": row["source"],
        }
        for seq, row in enumerate(visible, start=1)
    ]
    return {
        "id": f"C-{claim['id']}",
        "family": FAMILY,
        "question": claim["question"],
        "expected_answer": None,
        "closes_at_tick": closes_at_tick,
        "deadline_ping_tick": closes_at_tick - 1,
        "lag_b": lag_b,
        "max_env_lag": 0,
        "inversion_confidence": 0.75,
        "a_sticky": False,
        "env_schedule": schedule,
        "draft_answer": claim["answer"],
        "draft_confidence": claim.get("confidence"),
    }


def verdict_for(result: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    winner = result.get("winner")
    base = {"claim_id": claim["id"], "question": claim["question"], "draft_answer": claim["answer"]}
    if not winner:
        return {**base, "verdict": "block", "reason": "no_supported_answer", "supported_answer": None}
    supported = winner.get("answer")
    detail = {
        "supported_answer": supported,
        "winner_from": winner.get("from"),
        "winner_confidence": winner.get("confidence"),
        "winner_env_seq": winner.get("env_seq"),
        "latest_env_seq": result.get("latest_env_seq"),
    }
    if int(winner.get("env_seq") or 0) < int(result.get("latest_env_seq") or 0):
        return {**base, **detail, "verdict": "block", "reason": "winner_is_stale"}
    if float(winner.get("confidence") or 0) < MIN_CONFIDENCE:
        return {**base, **detail, "verdict": "block", "reason": "low_confidence"}
    if not answers_match(claim["answer"], supported):
        return {**base, **detail, "verdict": "block", "reason": "sources_disagree"}
    return {**base, **detail, "verdict": "pass", "reason": "sources_agree"}


def check_claims(
    claims: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    out_dir: Path,
    *,
    closes_at_tick: int = 8,
    lag_b: int = 2,
) -> dict[str, Any]:
    """Run the desk over every claim; the draft passes only if every claim does."""
    out_dir = Path(out_dir)
    verdicts = []
    for claim in claims:
        task = build_task(claim, timeline, closes_at_tick=closes_at_tick, lag_b=lag_b)
        arena_dir = out_dir / task["id"]
        result = Arena(arena_dir, task).run()
        verdict = verdict_for(result, claim)
        verdict["arena"] = str(arena_dir)
        verdicts.append(verdict)
    report = {
        "pass": bool(verdicts) and all(v["verdict"] == "pass" for v in verdicts),
        "claims": verdicts,
    }
    write_json(out_dir / "desk.json", report)
    return report


def final_claims(arena_dir: Path, task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each claimant's last non-retracted claim at close."""
    close = int(task["closes_at_tick"])
    rows = [r for r in load_jsonl(Path(arena_dir) / "ledger.jsonl") if r.get("task_id") == task["id"]]
    retracted = {r["parent_id"] for r in rows if r.get("type") == "retract" and r.get("parent_id")}
    last: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("type") == "claim" and int(row.get("created_at_tick", 0)) <= close and row["id"] not in retracted:
            last[row["from"]] = row
    return last


def resolve(arena_dir: Path, truth: Any) -> dict[str, Any]:
    """Re-score a closed desk task from its ledger once the truth is known."""
    arena_dir = Path(arena_dir)
    task = json.loads((arena_dir / "task.json").read_text(encoding="utf-8"))
    task = {**task, "expected_answer": truth}
    result = score_trial(arena_dir, task)
    briers = {}
    for agent, claim in final_claims(arena_dir, task).items():
        hit = 1.0 if answers_match((claim.get("payload") or {}).get("answer"), truth) else 0.0
        briers[agent] = round((float(claim.get("confidence") or 0) - hit) ** 2, 4)
    draft_hit = 1.0 if answers_match(task.get("draft_answer"), truth) else 0.0
    draft_conf = task.get("draft_confidence")
    resolution = {
        "task_id": task["id"],
        "truth": truth,
        "winner_correct": result["correct"],
        "confidence_inversion": result["confidence_inversion"],
        "draft_correct": bool(draft_hit),
        "brier": briers,
        "draft_brier": None if draft_conf is None else round((float(draft_conf) - draft_hit) ** 2, 4),
    }
    write_json(arena_dir / "resolution.json", resolution)
    return resolution


def calibration(resolutions: list[dict[str, Any]], authors: dict[str, str] | None = None) -> dict[str, Any]:
    """Mean Brier per verifier and per draft author (lower is better; 0.25 is a coin flip at 0.5)."""
    per_agent: dict[str, list[float]] = {}
    per_author: dict[str, list[float]] = {}
    for res in resolutions:
        for agent, score in res["brier"].items():
            per_agent.setdefault(agent, []).append(score)
        author = (authors or {}).get(res["task_id"], "unknown")
        if res.get("draft_brier") is not None:
            per_author.setdefault(author, []).append(res["draft_brier"])
    mean = lambda xs: round(sum(xs) / len(xs), 4)  # noqa: E731
    return {
        "resolved": len(resolutions),
        "winner_accuracy": round(sum(1 for r in resolutions if r["winner_correct"]) / (len(resolutions) or 1), 4),
        "verifiers": {k: {"n": len(v), "brier": mean(v)} for k, v in sorted(per_agent.items())},
        "authors": {k: {"n": len(v), "brier": mean(v)} for k, v in sorted(per_author.items())},
    }
