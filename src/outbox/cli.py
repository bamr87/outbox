"""CLI: init an arena, run policy bots, watch a ledger, score a closed trial."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .arena import Arena, new_run_dir
from .mailbox import Relay
from .score import load_jsonl, score_trial, summarize


def load_task(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_init(args: argparse.Namespace) -> int:
    task = load_task(Path(args.task))
    root = Path(args.arena)
    root.mkdir(parents=True, exist_ok=True)
    Arena(root, task).boot()
    print(f"initialized {root} task={task['id']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    task = load_task(Path(args.task))
    if args.lag_b is not None:
        task["lag_b"] = int(args.lag_b)
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + task["id"]
    root = new_run_dir(Path(args.out), run_id)
    result = Arena(root, task).run()
    print(json.dumps(result, indent=2))
    print(f"arena={root}", file=sys.stderr)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    rows = load_jsonl(Path(args.ledger))
    for row in rows:
        event = row.get("event") or row.get("type")
        if event == "tick":
            print(f"t+{row.get('tick')}  tick")
            continue
        kind = row.get("type")
        who = row.get("from")
        if kind == "env_event":
            fact = (row.get("payload") or {}).get("fact")
            print(f"t+{row.get('created_at_tick')}  env={row.get('env_seq')}  {fact}")
        elif kind == "claim":
            ans = (row.get("payload") or {}).get("answer")
            print(
                f"t+{row.get('created_at_tick')}  {who} claim  conf={row.get('confidence')}  "
                f"env={row.get('env_seq')}  ans={ans}"
            )
        elif kind == "retract":
            print(f"t+{row.get('created_at_tick')}  {who} retract  parent={row.get('parent_id')}")
        elif kind == "deadline_ping":
            print(f"t+{row.get('created_at_tick')}  deadline ping  closes_at={row.get('closes_at_tick')}")
        elif kind == "task_open":
            print(f"t+{row.get('created_at_tick')}  task_open  {(row.get('payload') or {}).get('question')}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    arena = Path(args.arena)
    task = json.loads((arena / "task.json").read_text(encoding="utf-8"))
    print(json.dumps(score_trial(arena, task), indent=2))
    return 0


def cmd_suite(args: argparse.Namespace) -> int:
    tasks_dir = Path(args.tasks)
    results = []
    out = Path(args.out)
    for task_path in sorted(tasks_dir.glob("*.json")):
        task = load_task(task_path)
        if args.lag_b is not None:
            task["lag_b"] = int(args.lag_b)
        root = new_run_dir(out, f"suite-{task['id']}")
        results.append(Arena(root, task).run())
    report = {"results": results, "summary": summarize(results)}
    print(json.dumps(report, indent=2))
    return 0


def cmd_relay(args: argparse.Namespace) -> int:
    delivered = Relay(Path(args.arena), ["scout", "claimant-a", "claimant-b"]).deliver_new()
    print(json.dumps({"delivered": delivered}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="outbox", description="Competitive agent outbox harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create mailbox dirs and drop task_open")
    p_init.add_argument("--task", required=True)
    p_init.add_argument("--arena", required=True)
    p_init.set_defaults(func=cmd_init)

    p_run = sub.add_parser("run", help="run three policy bots on a task")
    p_run.add_argument("--task", required=True)
    p_run.add_argument("--out", default=".")
    p_run.add_argument("--run-id")
    p_run.add_argument("--lag-b", type=int, default=None)
    p_run.set_defaults(func=cmd_run)

    p_watch = sub.add_parser("watch", help="pretty-print a ledger.jsonl")
    p_watch.add_argument("--ledger", required=True)
    p_watch.set_defaults(func=cmd_watch)

    p_score = sub.add_parser("score", help="score a finished arena directory")
    p_score.add_argument("--arena", required=True)
    p_score.set_defaults(func=cmd_score)

    p_suite = sub.add_parser("suite", help="run every task in a directory")
    p_suite.add_argument("--tasks", default="tasks")
    p_suite.add_argument("--out", default=".")
    p_suite.add_argument("--lag-b", type=int, default=None)
    p_suite.set_defaults(func=cmd_suite)

    p_relay = sub.add_parser("relay", help="deliver any undelivered outbox files")
    p_relay.add_argument("--arena", required=True)
    p_relay.set_defaults(func=cmd_relay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
