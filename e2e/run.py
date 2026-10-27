#!/usr/bin/env python3
"""End-to-end proof of the Outbox-Lifehack platform plan, run locally.

One piece of content and one Wire dispatch travel Sense -> Plan -> Produce ->
Verify -> Publish -> Learn on the outbox belt. Each stage uses the real tooling
of the repo that owns it, against throwaway copies of each repo's working tree
(uncommitted changes included; E2E_SNAPSHOT=head tests HEAD instead, see
snapshot.py), so no checkout is modified:

  scripts        sandboxed toolbelt smoke (adapters/scripts_toolbelt.sh)
  lifehacker.dev backlog race with its own scout/merge/lint code, its content
                 gates, and its overlay Jekyll build
  outbox         the belt, the claim desk, resolution and calibration
  zer0-cms       import/export round trip of the site snapshot
  gitorio        compile the pipeline's lines with the real compiler

Usage: python3 e2e/run.py [--projects DIR] [--out DIR] [--skip STAGE ...]
Each stage records pass / fail / skip with evidence in <out>/report.json and
<out>/report.md. A skipped stage says why; nothing is reported as proven unless
it ran.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from outbox.belt import Belt, Handoff, Station, to_cloudevent, write_belt_summary  # noqa: E402
from outbox.claimdesk import calibration, check_claims, resolve  # noqa: E402
from outbox.mailbox import append_jsonl, write_json  # noqa: E402
from snapshot import copy as copy_working_tree, state as repo_state  # noqa: E402

STATIONS = ("scout-a", "scout-b", "wire-scout", "planner", "drafter", "verifier", "editor", "publisher", "learner")
REPOS = ("outbox", "scripts", "lifehacker.dev", "zer0-cms", "gitorio")
TODAY = datetime.now(timezone.utc).date()


# --------------------------------------------------------------------------- helpers


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def slugify(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-")


def snapshot(repo: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    return copy_working_tree(repo, dest)


class Run:
    def __init__(self, projects: Path, out: Path, skip: set[str]) -> None:
        self.projects = projects
        self.out = out
        self.skip = skip
        self.stages: list[dict[str, Any]] = []
        self.state: dict[str, Any] = {}
        self.belt = Belt(out / "belt", STATIONS)

    def repo(self, name: str) -> Path:
        return self.projects / name

    def adapter(self, script: str, *args: Any, timeout: int = 2400, env: dict[str, str] | None = None) -> dict[str, Any]:
        log_dir = self.out / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        proc = subprocess.run(
            [str(HERE / "adapters" / script), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )
        (log_dir / f"{script}.{int(started)}.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip().startswith("{")]
        try:
            summary = json.loads(lines[-1]) if lines else {"ok": False, "error": "adapter printed no JSON summary"}
        except json.JSONDecodeError as exc:
            summary = {"ok": False, "error": f"bad summary JSON: {exc}"}
        summary["_exit"] = proc.returncode
        summary["_seconds"] = round(time.time() - started, 1)
        return summary

    def stage(self, name: str, title: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        record: dict[str, Any] = {"stage": name, "title": title}
        started = time.time()
        if name in self.skip:
            record.update(status="skip", reason="skipped with --skip")
        else:
            print(f"[e2e] {name}: {title} ...", flush=True)
            try:
                record.update(fn())
            except Exception as exc:  # a stage failure is evidence, not a crash of the whole proof
                record.update(status="error", error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc())
        record["seconds"] = round(time.time() - started, 1)
        self.stages.append(record)
        print(f"[e2e] {name}: {record['status']} ({record['seconds']}s)", flush=True)
        return record

    def send(self, station: str, msg: Handoff) -> Handoff:
        self.belt.station(station).send(msg)
        return msg


def _scout_worker(belt_root: str, station: str, ideas: list[dict[str, Any]]) -> None:
    box = Station(Path(belt_root), station)
    for idea in ideas:
        box.send(
            Handoff(
                kind="idea",
                from_station=station,
                to=["planner", "ledger"],
                subject=fingerprint("scout", idea["collection"], idea["title"]),
                payload=idea,
            )
        )


# --------------------------------------------------------------------------- stages


def stage_preflight(run: Run) -> dict[str, Any]:
    repos = {}
    for name in REPOS:
        path = run.repo(name)
        if not (path / ".git").exists():
            repos[name] = {"present": False}
            continue
        branch = subprocess.run(["git", "-C", str(path), "branch", "--show-current"], capture_output=True, text=True)
        repos[name] = {"present": True, "branch": branch.stdout.strip(), **repo_state(path)}
    docker = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
    run.state["docker"] = docker.returncode == 0
    missing = [n for n, r in repos.items() if not r["present"]]
    return {
        "status": "pass" if not missing and run.state["docker"] else "fail",
        "repos": repos,
        "docker": docker.stdout.strip() or None,
        "missing": missing,
    }


def stage_outbox_tests(run: Run) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=HERE.parent,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(HERE.parent / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )
    tail = proc.stderr.strip().splitlines()[-3:]
    ran = re.search(r"Ran (\d+) tests", proc.stderr)
    return {"status": "pass" if proc.returncode == 0 else "fail", "tests": int(ran.group(1)) if ran else 0, "tail": tail}


def stage_toolbelt(run: Run) -> dict[str, Any]:
    summary = run.adapter("scripts_toolbelt.sh", run.repo("scripts"), run.out / "toolbelt")
    # The safety contract the plan's Phase 0 restored: --help is side-effect free and
    # CREATE_GITHUB_REPO=false never reaches `gh repo create`.
    unsafe = [f for f in summary.get("findings", []) if f.get("rule") in ("help-fails", "help-has-side-effects", "ignores-CREATE_GITHUB_REPO=false")]
    return {
        "status": "pass" if summary.get("shellcheck_clean") and summary.get("_exit") == 0 and not unsafe else "fail",
        "summary": {k: summary.get(k) for k in ("shellcheck_clean", "forkme_metadata_dry_run", "zer0_site_type_supported", "gh_calls_attempted")},
        "findings": summary.get("findings", []),
    }


def stage_sense(run: Run) -> dict[str, Any]:
    ideas = json.loads((HERE / "fixtures" / "ideas.json").read_text())
    story = json.loads((HERE / "fixtures" / "wire_story.json").read_text())
    ctx = multiprocessing.get_context("spawn")
    workers = [
        ctx.Process(target=_scout_worker, args=(str(run.belt.root), station, ideas[station]))
        for station in ("scout-a", "scout-b")
    ]
    for proc in workers:
        proc.start()
    wire_idea = run.send(
        "wire-scout",
        Handoff(
            kind="idea",
            from_station="wire-scout",
            to=["planner", "ledger"],
            subject=fingerprint("wire", story["idea"]["title"]),
            payload={"collection": "wire", **story["idea"]},
        ),
    )
    for proc in workers:
        proc.join(60)
    delivered = run.belt.deliver()
    run.state["wire_trace"] = wire_idea.trace
    return {
        "status": "pass" if len(delivered) == 5 and all(p.exitcode == 0 for p in workers) else "fail",
        "ideas_delivered": len(delivered),
        "senders": sorted({d["from"] for d in delivered}),
    }


def stage_plan(run: Run) -> dict[str, Any]:
    ideas = run.belt.station("planner").receive(kinds=("idea",))
    content = [m for m in ideas if m.payload.get("collection") != "wire"]
    wire = [m for m in ideas if m.payload.get("collection") == "wire"]
    race_dir = run.out / "race"
    race_dir.mkdir(parents=True, exist_ok=True)
    by_scout: dict[str, list[dict[str, Any]]] = {"scout-a": [], "scout-b": []}
    for msg in content:
        by_scout[msg.from_station].append(msg.payload)
    for name, rows in (("a", by_scout["scout-a"]), ("b", by_scout["scout-b"]), ("belt", [m.payload for m in content])):
        (race_dir / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    race = run.adapter(
        "lifehacker_backlog_race.sh",
        run.repo("lifehacker.dev"),
        race_dir / "a.jsonl",
        race_dir / "b.jsonl",
        race_dir / "belt.jsonl",
        race_dir / "out",
    )
    minted: dict[str, str] = {}
    mint_log = race_dir / "out" / "mint-planner.txt"
    if mint_log.exists():
        for line in mint_log.read_text().splitlines():
            m = re.match(r"\s*\+ (SRC-\d+) \((\w+)\) (.+?)\s+<- ", line)
            if m:
                minted[m.group(3).strip()] = m.group(1)
    assignments = []
    # The factory drafts one piece per kind per run; the hack template is written for the port-8080 idea.
    hack = next((m for m in content if "port 8080" in m.payload["title"]), None)
    for msg in content:
        backlog_id = minted.get(msg.payload["title"])
        assignment = msg.reply(
            "assignment",
            "planner",
            ["drafter", "ledger"],
            {"backlog_id": backlog_id, "kind": msg.payload["collection"], "title": msg.payload["title"],
             "draft_now": msg is hack},
        )
        run.send("planner", assignment)
        assignments.append(backlog_id)
    for msg in wire:
        run.send(
            "planner",
            msg.reply("assignment", "planner", ["drafter", "ledger"],
                      {"backlog_id": "WIRE-E2E-001", "kind": "wire", "title": msg.payload["title"], "author": "rhea", "draft_now": True}),
        )
    run.belt.deliver()
    belt_ok = bool(race.get("belt")) and not race["belt"].get("duplicate_ids") and race["belt"].get("lint_artifacts_exit") == 0
    branches_collide = bool((race.get("branches") or {}).get("same_ids_minted"))
    return {
        "status": "pass" if belt_ok and len(minted) == len(content) else "fail",
        "race": {k: race.get(k) for k in ("branches", "union", "belt")},
        "branches_collide_without_belt": branches_collide,
        "assignments": assignments + ["WIRE-E2E-001"],
    }


def render_post(kind: str, values: dict[str, Any]) -> str:
    template = Template((HERE / "fixtures" / "posts" / f"{kind}.md").read_text())
    return template.substitute(values)


def draw_preview(run: Run, rel: str) -> list[str]:
    """Run the site's own offline banner generator on one post; return the files it added."""
    site = run.state["site"]
    previews = site / "assets" / "images" / "previews"
    before = set(previews.glob("*")) if previews.exists() else set()
    subprocess.run(
        ["docker", "run", "--rm", "--network=none", "-v", f"{site}:/repo", "-w", "/repo", "node:22-slim",
         "node", "scripts/preview/generate.mjs", "-f", rel],
        capture_output=True, text=True, check=True, timeout=300,
    )
    return sorted(str(p.relative_to(site)) for p in set(previews.glob("*")) - before)


def write_wire_post(run: Run, claims: list[dict[str, Any]], revision: int) -> Path:
    story = json.loads((HERE / "fixtures" / "wire_story.json").read_text())
    by_id = {c["id"]: c for c in claims}
    site = run.state["site"]
    rel = run.state.setdefault("wire_rel", f"pages/_posts/wire/{TODAY.isoformat()}-quokka-q7-e2e.md")
    claims_yaml = "\n".join(
        f"  - id: {c['id']}\n    question: \"{c['question']}\"\n    answer: \"{c['answer']}\"\n"
        f"    confidence: {c['confidence']}\n    sources: [{', '.join(c['sources'])}]"
        for c in claims
    )
    text = render_post(
        "wire",
        {
            "date": TODAY.isoformat(),
            "title": story["idea"]["title"].replace('"', "'"),
            "sources_yaml": "\n".join(f"  - {s['url']}" for s in story["sources"].values()),
            "claims_yaml": claims_yaml,
            "context": by_id["q7-context"]["answer"],
            "license": by_id["q7-license"]["answer"],
            "weights_date": by_id["q7-weights-date"]["answer"],
            "url_model_card": story["sources"]["model-card"]["url"],
            "url_leaderboard": story["sources"]["leaderboard"]["url"],
            "url_vendor_blog": story["sources"]["vendor-blog"]["url"],
            "revision": revision,
        },
    )
    path = site / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    run.state.setdefault("changed", set()).update([rel, *draw_preview(run, rel)])
    return path


def stage_produce(run: Run) -> dict[str, Any]:
    site = snapshot(run.repo("lifehacker.dev"), run.out / "site")
    run.state["site"] = site
    assignments = run.belt.station("drafter").receive(kinds=("assignment",))
    to_draft = [a for a in assignments if a.payload.get("draft_now")]
    drafts = []
    for a in to_draft:
        if a.payload["kind"] == "wire":
            story = json.loads((HERE / "fixtures" / "wire_story.json").read_text())
            claims = [dict(c) for c in story["claims"]]
            run.state["wire_claims"] = claims
            path = write_wire_post(run, claims, revision=1)
            payload = {"kind": "wire", "author": "rhea", "tier": "factual", "backlog_id": a.payload["backlog_id"], "claims": [c["id"] for c in claims]}
            run.state["wire_assignment"] = a
        else:
            title = a.payload["title"]
            rel = f"pages/_posts/hacks/{TODAY.isoformat()}-find-the-process-on-port-8080.md"
            path = site / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_post("hack", {"date": TODAY.isoformat(), "title": title.replace('"', "'")}))
            run.state.setdefault("changed", set()).update([rel, *draw_preview(run, rel)])
            payload = {"kind": "hack", "author": "edge", "tier": "low-risk", "backlog_id": a.payload["backlog_id"]}
            run.state["hack_rel"] = rel
        rel_path = str(path.relative_to(site))
        draft = a.reply("draft", "drafter", ["verifier", "ledger"], payload, ref={"path": rel_path, "sha256": sha256(path)})
        run.send("drafter", draft)
        drafts.append({"kind": payload["kind"], "path": rel_path})
    run.belt.deliver()
    return {"status": "pass" if len(drafts) == 2 else "fail", "drafts": drafts}


def gate_files(run: Run, tag: str, files: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    """lifehacker.dev's own harness, scoped to the changed files the way `verify` scopes a pull request."""
    out = run.out / "gates" / tag
    changed = sorted(run.state.get("changed", set()) | set(files))
    summary = run.adapter("lifehacker_gates.sh", run.state["site"], out, *changed, env=env)
    findings = []
    findings_path = Path(summary.get("findings_jsonl") or out / "test-results" / "findings.jsonl")
    if findings_path.exists():
        for line in findings_path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if any(f in str(row.get("file") or "") for f in files):
                    findings.append({k: row.get(k) for k in ("check_id", "severity", "rule", "file", "evidence")})
    return {
        "adapter": {k: summary.get(k) for k in ("ok", "gate", "scoped", "checks_run", "checks_skipped", "by_severity", "_exit")},
        "ok": bool(summary.get("ok")),
        "new_file_findings": findings,
        "blocking": summary.get("errors") or [],
    }


def stage_verify(run: Run) -> dict[str, Any]:
    drafts = run.belt.station("verifier").receive(kinds=("draft",))
    files = [d.ref["path"] for d in drafts]
    gates_first = gate_files(run, "first", files)
    story = json.loads((HERE / "fixtures" / "wire_story.json").read_text())
    wire_draft = next(d for d in drafts if d.payload["kind"] == "wire")
    hack_draft = next(d for d in drafts if d.payload["kind"] == "hack")

    # The Wire is rhea's desk: the same dispatch under another persona's byline must fail the gates.
    wire_path = run.state["site"] / wire_draft.ref["path"]
    decoy = wire_path.with_name(f"{TODAY.isoformat()}-byline-decoy.md")
    decoy.write_text(re.sub(r"(?m)^author: rhea$", "author: edge", wire_path.read_text()))
    try:
        gates_decoy = gate_files(run, "byline-decoy", [str(decoy.relative_to(run.state["site"]))])
    finally:
        decoy.unlink()
    byline_enforced = any(f["rule"] == "wrong-desk-byline" for f in gates_decoy["new_file_findings"])

    desk_first = check_claims(run.state["wire_claims"], story["timeline"], run.out / "desk" / "first")
    rounds = [{"round": 1, "pass": desk_first["pass"], "claims": [{k: c[k] for k in ("claim_id", "verdict", "reason", "draft_answer", "supported_answer")} for c in desk_first["claims"]]}]
    block = wire_draft.reply("verdict", "verifier", ["drafter", "learner", "ledger"],
                             {"verdict": "block", "desk": rounds[0]["claims"]})
    run.send("verifier", block)
    run.belt.deliver()

    # The drafter takes the block, corrects every claim the sources no longer support, and re-drafts.
    verdicts = run.belt.station("drafter").receive(kinds=("verdict",))
    claims = run.state["wire_claims"]
    corrected = []
    for v in verdicts:
        for c in v.payload["desk"]:
            if c["verdict"] == "block" and c["supported_answer"]:
                for claim in claims:
                    if claim["id"] == c["claim_id"]:
                        claim["answer"] = c["supported_answer"]
                        claim["confidence"] = 0.85
                        corrected.append(claim["id"])
    path = write_wire_post(run, claims, revision=2)
    redraft = verdicts[0].reply("draft", "drafter", ["verifier", "ledger"], {**wire_draft.payload, "revision": 2, "corrected": corrected},
                                ref={"path": str(path.relative_to(run.state["site"])), "sha256": sha256(path)})
    run.send("drafter", redraft)
    run.belt.deliver()
    run.belt.station("verifier").receive(kinds=("draft",))

    gates_second = gate_files(run, "second", files)
    desk_second = check_claims(claims, story["timeline"], run.out / "desk" / "second")
    run.state["desk_second"] = desk_second
    rounds.append({"round": 2, "pass": desk_second["pass"], "claims": [{k: c[k] for k in ("claim_id", "verdict", "reason", "draft_answer", "supported_answer")} for c in desk_second["claims"]]})

    hack_ok = gates_second["ok"] and not [f for f in gates_second["blocking"] if hack_draft.ref["path"] in str(f.get("file"))]
    wire_ok = desk_second["pass"] and gates_second["ok"] and not [f for f in gates_second["blocking"] if wire_draft.ref["path"] in str(f.get("file"))]
    # Merge by risk tier (decision D1): low-risk content goes straight to the publisher; factual content needs an editor.
    run.send("verifier", hack_draft.reply("verdict", "verifier", ["publisher", "ledger"], {"verdict": "pass" if hack_ok else "block", "tier": "low-risk"}))
    run.send("verifier", redraft.reply("verdict", "verifier", ["editor", "learner", "ledger"], {"verdict": "pass" if wire_ok else "block", "tier": "factual"}))
    run.belt.deliver()
    for v in run.belt.station("editor").receive(kinds=("verdict",)):
        if v.payload["verdict"] == "pass":
            run.send("editor", v.reply("approval", "editor", ["publisher", "ledger"],
                                       {"by": "simulated-human", "note": "The e2e stands in for the human merge a factual piece requires."}))
    run.belt.deliver()
    return {
        "status": "pass" if hack_ok and wire_ok and not desk_first["pass"] and byline_enforced else "fail",
        "gates_first": gates_first,
        "byline_decoy": {"enforced": byline_enforced, "findings": gates_decoy["new_file_findings"]},
        "gates_second": gates_second,
        "claim_desk_rounds": rounds,
        "corrected_claims": corrected,
    }


def stage_publish(run: Run) -> dict[str, Any]:
    inbox = run.belt.station("publisher").receive()
    ready = [m for m in inbox if (m.kind == "verdict" and m.payload.get("verdict") == "pass") or m.kind == "approval"]
    site = run.state["site"]
    posts = {"hack": run.state.get("hack_rel"), "wire": run.state.get("wire_rel")}
    build = run.adapter("lifehacker_build.sh", site, run.out / "build", *[p for p in posts.values() if p])
    site_dir = Path(build.get("site_dir") or run.out / "build" / "_site")
    rendered = {}
    for kind, rel in posts.items():
        hit = next((p for p in build.get("posts") or [] if p.get("source") == rel), {})
        rendered[kind] = {"url": hit.get("url"), "exists": hit.get("exists"), "title_found": hit.get("title_found")}
    # Full `verify` parity: the same harness again, now with the built site, so html-proofer checks every link.
    proofed = gate_files(run, "with-site", [p for p in posts.values() if p],
                         env={"LH_SITE_DIR": str(site_dir), "LH_BUILD_RC": "0" if build.get("ok") else "1"})
    roundtrip = run.adapter("zer0cms_roundtrip.sh", site, run.out / "zer0cms")
    events = run.out / "events.jsonl"
    published = []
    for m in ready:
        kind = "wire" if m.kind == "approval" else "hack"
        url = rendered[kind]["url"] if rendered[kind].get("exists") and rendered[kind].get("title_found") else None
        if url:
            msg = m.reply("published", "publisher", ["learner", "ledger"], {"kind": kind, "canonical_url": url})
            run.send("publisher", msg)
            append_jsonl(events, to_cloudevent(msg.to_dict()))
            published.append({"kind": kind, "url": url})
    run.belt.deliver()
    ok = bool(build.get("ok")) and len(published) == 2 and proofed["ok"]
    return {
        "status": "pass" if ok and roundtrip.get("ok") else ("fail" if not ok else "partial"),
        "build": {k: build.get(k) for k in ("ok", "pages", "_seconds", "_exit")},
        "verify_with_site": {k: proofed[k] for k in ("ok", "adapter", "blocking")},
        "rendered": rendered,
        "published": published,
        "zer0cms_roundtrip": {k: roundtrip.get(k) for k in ("ok", "files_total", "files_identical", "files_different", "files_unsupported", "diff_samples", "events", "_exit", "error")},
    }


def stage_learn(run: Run) -> dict[str, Any]:
    story = json.loads((HERE / "fixtures" / "wire_story.json").read_text())
    desk = run.state.get("desk_second") or {"claims": []}
    resolutions = [resolve(Path(c["arena"]), story["truth"][c["claim_id"]]) for c in desk["claims"]]
    cal = calibration(resolutions, {r["task_id"]: "rhea" for r in resolutions})
    recheck = check_claims(run.state["wire_claims"], story["timeline"], run.out / "desk" / "recheck", closes_at_tick=28)
    stale = [c for c in recheck["claims"] if c["verdict"] == "block"]
    findings = run.out / "findings.jsonl"
    signals = []
    for c in stale:
        row = {
            "check_id": "claim-desk",
            "severity": "error",
            "file": run.state.get("wire_rel"),
            "line": None,
            "rule": "stale-published-claim",
            "evidence": f"{c['claim_id']}: published {c['draft_answer']!r}, sources now say {c['supported_answer']!r}",
            "route_to": "lifehacker.dev",
        }
        row["fingerprint"] = fingerprint(row["check_id"], str(row["file"]), row["rule"], c["claim_id"])
        append_jsonl(findings, row)
        signal = Handoff(kind="signal", from_station="learner", to=["planner", "ledger"], subject=c["claim_id"],
                         trace=run.state["wire_trace"], payload={"signal": "correction", **row})
        run.send("learner", signal)
        signals.append(row["evidence"])
    run.belt.deliver()
    corrections = []
    for sig in run.belt.station("planner").receive(kinds=("signal",)):
        msg = sig.reply("assignment", "planner", ["drafter", "ledger"], {"kind": "correction", "backlog_id": "WIRE-E2E-001", "claim": sig.subject})
        run.send("planner", msg)
        corrections.append(sig.subject)
    run.belt.deliver()
    write_json(run.out / "calibration.json", cal)
    return {
        "status": "pass" if stale and corrections else "fail",
        "resolutions": resolutions,
        "calibration": cal,
        "post_publish_recheck": [{k: c[k] for k in ("claim_id", "verdict", "draft_answer", "supported_answer")} for c in recheck["claims"]],
        "correction_assignments": corrections,
    }


def stage_orchestrate(run: Run) -> dict[str, Any]:
    results = {}
    blueprints = sorted((HERE / "blueprints").glob("*.blueprint.json"))
    lh = run.repo("lifehacker.dev") / ".factory" / "issue-factory.blueprint.json"
    targets = [*blueprints, lh] if lh.exists() else blueprints
    for bp in targets:
        name = bp.name if bp.parent == HERE / "blueprints" else f"lifehacker.dev:{bp.name}"
        out = run.out / "gitorio" / slugify(name)
        summary = run.adapter("gitorio_compile.sh", bp, out)
        results[name] = {k: summary.get(k) for k in ("ok", "workflows", "errors", "guards", "_exit")}
        # A line woken by repository_dispatch must check its payload before anything else runs (only the number crosses).
        if '"trigger.repository_dispatch"' in bp.read_text():
            results[name]["payload_checked"] = any("dispatch.payload" in y.read_text() for y in out.glob("*.yml"))
    ours_ok = all(r["ok"] for n, r in results.items() if not n.startswith("lifehacker.dev:"))
    lh_ok = next((r["ok"] for n, r in results.items() if n.startswith("lifehacker.dev:")), None)
    return {"status": "pass" if ours_ok and results else "fail", "lifehacker_issue_factory_compiles": lh_ok, "compiles": results}


def stage_ledger(run: Run) -> dict[str, Any]:
    summary = write_belt_summary(run.belt, run.out / "belt-summary.json")
    journey = [{"seq": r["seq"], "from": r["from"], "kind": r["kind"], "to": r["to"]} for r in run.belt.journey(run.state.get("wire_trace", ""))]
    ok = summary["seq_gapless"] and summary["ids_unique"] and summary["dead_letters"] == 0
    return {"status": "pass" if ok else "fail", "belt": summary, "wire_journey": journey}


STAGES: list[tuple[str, str, Callable[[Run], dict[str, Any]]]] = [
    ("preflight", "Sibling repos and Docker are present", stage_preflight),
    ("outbox-tests", "outbox unit tests, including the belt and claim desk", stage_outbox_tests),
    ("toolbelt", "scripts: sandboxed toolbelt smoke", stage_toolbelt),
    ("sense", "Concurrent scouts hand ideas to the planner on the belt", stage_sense),
    ("plan", "Planner mints backlog ids as the single writer (vs. the branch race)", stage_plan),
    ("produce", "Drafter writes a hack and a Wire dispatch into a lifehacker.dev snapshot", stage_produce),
    ("verify", "lifehacker.dev gates + the claim desk; correction loop; risk-tier approval", stage_verify),
    ("publish", "Jekyll build, zer0-cms round trip, published events", stage_publish),
    ("learn", "Resolve claims, calibrate, recheck after publication, route a correction", stage_learn),
    ("orchestrate", "gitorio compiles the pipeline's lines", stage_orchestrate),
    ("ledger", "One gapless ledger traces the whole journey", stage_ledger),
]


def write_report(run: Run) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for s in run.stages:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "counts": counts, "stages": run.stages}
    write_json(run.out / "report.json", report)
    lines = ["# Outbox-Lifehack end-to-end proof", "", f"> Generated {report['generated_at']}; {counts}.", ""]
    lines += ["| Stage | Result | Seconds | What it shows |", "| --- | --- | --- | --- |"]
    for s in run.stages:
        lines.append(f"| {s['stage']} | {s['status']} | {s['seconds']} | {s['title']} |")
    (run.out / "report.md").write_text("\n".join(lines) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--projects", default=str(HERE.parents[1]), help="directory holding the sibling repos")
    parser.add_argument("--out", default=str(HERE.parent / "runs" / ("e2e-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))))
    parser.add_argument("--skip", nargs="*", default=[], help="stage names to skip")
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    run = Run(Path(args.projects).resolve(), out, set(args.skip))
    for name, title, fn in STAGES:
        run.stage(name, title, lambda fn=fn: fn(run))
    report = write_report(run)
    print(json.dumps(report["counts"]))
    print(f"report: {out / 'report.md'}")
    return 0 if all(s["status"] in {"pass", "skip"} for s in run.stages) else 1


if __name__ == "__main__":
    raise SystemExit(main())
