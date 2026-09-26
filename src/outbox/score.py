"""Close a task: pick a winner from the ledger, score against ground truth."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def active_claims(rows: list[dict[str, Any]], task_id: str, close_tick: int) -> list[dict[str, Any]]:
    claims: dict[str, dict[str, Any]] = {}
    retracted: set[str] = set()
    for row in rows:
        if row.get("task_id") != task_id:
            continue
        tick = int(row.get("created_at_tick", 0))
        if tick > close_tick:
            continue
        kind = row.get("type")
        if kind == "claim":
            claims[row["id"]] = row
        elif kind == "retract" and row.get("parent_id"):
            retracted.add(row["parent_id"])
    return [claim for cid, claim in claims.items() if cid not in retracted]


def pick_winner(
    claims: list[dict[str, Any]],
    latest_env_seq: int,
    max_lag: int = 0,
) -> dict[str, Any] | None:
    eligible = [c for c in claims if int(c.get("env_seq", -1)) >= latest_env_seq - max_lag]
    pool = eligible or claims
    if not pool:
        return None
    return max(
        pool,
        key=lambda c: (float(c.get("confidence") or 0), int(c.get("env_seq") or 0), int(c.get("created_at_tick") or 0)),
    )


def answers_match(predicted: Any, expected: Any) -> bool:
    if expected is None:
        return False
    if isinstance(expected, str) and isinstance(predicted, str):
        return predicted.strip().lower() == expected.strip().lower()
    return predicted == expected


def score_trial(arena_root: Path, task: dict[str, Any]) -> dict[str, Any]:
    rows = load_jsonl(arena_root / "ledger.jsonl")
    task_id = task["id"]
    close_tick = int(task["closes_at_tick"])
    latest_env = 0
    for row in rows:
        if row.get("type") == "env_event" and row.get("task_id") == task_id:
            latest_env = max(latest_env, int(row.get("env_seq") or 0))
    claims = active_claims(rows, task_id, close_tick)
    winner = pick_winner(claims, latest_env, max_lag=int(task.get("max_env_lag", 0)))
    expected = task.get("expected_answer")
    correct = bool(winner) and answers_match((winner or {}).get("payload", {}).get("answer"), expected)
    inversion = bool(winner) and not correct and float((winner or {}).get("confidence") or 0) >= float(
        task.get("inversion_confidence", 0.75)
    )
    first = None
    if claims:
        first = min(claims, key=lambda c: int(c.get("created_at_tick") or 0))
    per_agent: dict[str, int] = defaultdict(int)
    retracts = 0
    for row in rows:
        if row.get("task_id") != task_id:
            continue
        if row.get("type") == "claim":
            per_agent[row.get("from", "?")] += 1
        if row.get("type") == "retract":
            retracts += 1
    return {
        "task_id": task_id,
        "family": task.get("family"),
        "latest_env_seq": latest_env,
        "n_active_claims": len(claims),
        "winner": None
        if not winner
        else {
            "from": winner.get("from"),
            "id": winner.get("id"),
            "confidence": winner.get("confidence"),
            "env_seq": winner.get("env_seq"),
            "answer": (winner.get("payload") or {}).get("answer"),
            "tick": winner.get("created_at_tick"),
        },
        "first_claim_from": None if not first else first.get("from"),
        "correct": correct,
        "confidence_inversion": inversion,
        "retracts": retracts,
        "claims_per_agent": dict(per_agent),
        "expected_answer": expected,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results) or 1
    wins = sum(1 for r in results if r.get("correct"))
    inversions = sum(1 for r in results if r.get("confidence_inversion"))
    return {
        "trials": len(results),
        "accuracy": wins / n,
        "inversion_rate": inversions / n,
        "mean_retracts": sum(r.get("retracts", 0) for r in results) / n,
        "winner_counts": _count(results, lambda r: (r.get("winner") or {}).get("from")),
        "first_claim_counts": _count(results, lambda r: r.get("first_claim_from")),
    }


def _count(results: list[dict[str, Any]], key) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for row in results:
        value = key(row) or "none"
        out[str(value)] += 1
    return dict(out)
