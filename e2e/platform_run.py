#!/usr/bin/env python3
"""A live end-to-end run that originates in the zer0-cms platform and lands in lifehacker.dev.

zer0-cms runs as its own Docker Compose stack (Postgres, Redis, the web role, the Celery worker and beat, the outbox
relay and the belt publisher). A person signs in to its API, and every piece of work starts there:

  sense        the person sends a hack idea and a Wire idea onto the belt from the platform's Outbox desk
  plan         lifehacker.dev's own backlog builders mint the ids (ruby:3.4); the planner assigns them on the belt
  produce      the person writes both pieces as draft pages in the platform: typed fields, raw front matter, file path
  verify       the platform's worker runs the claim desk (outbox) on the dispatch; a block sends it back, the person
               corrects the draft and it passes; lifehacker.dev's gates check the exported drafts; the person approves
  publish      the person publishes in the platform; its transactional outbox, relay and belt publisher announce each
               page on the belt; the platform exports the site and the new files land on a lifehacker.dev branch with
               their cover art; lifehacker.dev gates and builds the site (Jekyll on the zer0-mistakes theme)
  learn        the platform resolves the claims and reports calibration; a later read of the sources blocks a
               published claim and the person signals a correction
  orchestrate  gitorio compiles the lines that run this pipeline on Actions (node:22)
  toolbelt     scripts' safety smoke and shellcheck (ruby:3.4, koalaman/shellcheck)
  harness      outbox's own suite (python:3.13)
  ledger       the belt traces each piece from idea to published

Everything runs in Docker. The host only drives HTTP and docker commands, and plays the planner, drafter and verifier
stations with the outbox library on the belt directory the platform shares; the platform's relay stays the belt's only
writer. Content is synthetic: the Wire story's company and model are fictional and its sources point at example.org,
so the lifehacker.dev branch this writes is test output, never to be published.

Usage: python3 e2e/platform_run.py [--projects DIR] [--out DIR] [--port N] [--keep-up]
Writes report.json and report.md under --out, and leaves the run's posts, cover art and backlog entries uncommitted on
a new lifehacker.dev branch `e2e/platform-run-<timestamp>`.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

from outbox.belt import Station  # noqa: E402
from snapshot import copy as copy_working_tree, state as repo_state  # noqa: E402

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
PROJECT = "e2e-zer0"
SITE_KEY = "lifehacker"
SITE_HOST = f"{SITE_KEY}.localhost"
REPOS = ("zer0-cms", "outbox", "lifehacker.dev", "gitorio", "scripts")
SERVICES = ("db", "redis", "content", "worker", "research-worker", "beat", "relay", "belt-publisher")
# The page fields zer0-cms's studio API takes for a file's raw front matter and its path (content.edit).
FRONT_MATTER_FIELD = "front_matter"
PATH_FIELD = "file_path"
HACK_PATH = f"pages/_posts/hacks/{TODAY.isoformat()}-find-the-process-on-port-8080.md"
WIRE_PATH = f"pages/_posts/wire/{TODAY.isoformat()}-quokka-q7-e2e.md"


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, payload: Any) -> None:
        super().__init__(f"{method} {path} -> {status}: {json.dumps(payload)[:600]}")
        self.status, self.payload = status, payload


class Platform:
    """zer0-cms as its Compose stack, and one person signed in to its API."""

    def __init__(self, zrepo: Path, projects: Path, out: Path, port: int) -> None:
        self.zrepo, self.out, self.port = zrepo, out / "platform", port
        self.belt_dir = self.out / "outbox-data"  # the platform's OUTBOX_OUT_DIR, bind-mounted
        self.belt_root = self.belt_dir / "belt"  # its OUTBOX_BELT_ROOT
        self.export_dir = self.out / "export"
        self.override = self.out / "compose.e2e.yml"
        self.env = {**os.environ, "SITES_DIR": str(projects), "CONTENT_PORT": str(port), "COMPOSE_PROJECT_NAME": PROJECT}
        self.base = f"http://127.0.0.1:{port}"
        self.jar = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.calls: list[dict[str, Any]] = []

    def write_override(self) -> None:
        for d in (self.belt_dir, self.export_dir):
            d.mkdir(parents=True, exist_ok=True)
        belt = f"{self.belt_dir}:/app/content/var/outbox"
        self.override.write_text(
            "# The e2e run's additions: the belt on a host directory (the host plays stations on it) and an export dir.\n"
            "services:\n"
            f"  content:\n    volumes:\n      - {belt}\n      - {self.export_dir}:/export\n"
            f"  worker:\n    volumes:\n      - {belt}\n"
            f"  belt-publisher:\n    volumes:\n      - {belt}\n"
        )

    def compose(self, *args: str, timeout: int = 2400, check: bool = True, log: str | None = None) -> subprocess.CompletedProcess:
        cmd = ["docker", "compose", "-f", str(self.zrepo / "docker-compose.yml"), "-f", str(self.override),
               "--profile", "content", *args]
        proc = subprocess.run(cmd, cwd=self.zrepo, env=self.env, capture_output=True, text=True, timeout=timeout)
        if log:
            (self.out / f"{log}.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        if check and proc.returncode:
            raise RuntimeError(f"docker compose {' '.join(args[:3])} failed ({proc.returncode}): {proc.stderr[-1500:]}")
        return proc

    def manage(self, *args: str, env: dict[str, str] | None = None, timeout: int = 2400, log: str | None = None) -> str:
        flags = [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
        return self.compose("exec", "-T", *flags, "content", "python", "manage.py", *args, timeout=timeout, log=log).stdout

    def api(self, method: str, path: str, body: Any = None, params: dict[str, Any] | None = None,
            expect: tuple[int, ...] = (200, 201, 202)) -> Any:
        url = f"{self.base}/api/v2/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Referer", f"{self.base}/studio/")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        token = next((c.value for c in self.jar if c.name == "csrftoken"), None)
        if token:
            req.add_header("X-CSRFToken", token)
        try:
            with self.http.open(req, timeout=120) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        payload = json.loads(raw) if raw else None
        self.calls.append({"method": method, "path": path, "status": status})
        if status not in expect:
            raise ApiError(method, path, status, payload)
        return payload

    def sign_in(self, username: str, password: str) -> dict[str, Any]:
        self.api("GET", "me", expect=(200, 401, 403))  # sets the csrftoken cookie
        return self.api("POST", "session/login", {"username": username, "password": password})

    def wait(self, path: str, done: Callable[[Any], bool], timeout: int = 300, every: float = 2.0) -> Any:
        deadline = time.time() + timeout
        while True:
            got = self.api("GET", path)
            if done(got):
                return got
            if time.time() > deadline:
                raise TimeoutError(f"{path} not done after {timeout}s: {json.dumps(got)[:400]}")
            time.sleep(every)

    def deliver(self) -> dict[str, Any]:
        self.api("POST", "outbox/belt/deliver/")
        return self.wait("outbox/belt/", lambda s: s["pending"] == 0, timeout=120)

    def wait_run(self, kind: str, run_id: str, field: str = "status", timeout: int = 600) -> dict[str, Any]:
        return self.wait(f"outbox/{kind}/{run_id}/", lambda r: r[field] in ("succeeded", "failed"), timeout=timeout)


class PlatformRun:
    def __init__(self, projects: Path, out: Path, port: int, keep_up: bool, resume: bool = False) -> None:
        self.projects, self.out, self.keep_up, self.resume = projects, out, keep_up, resume
        self.platform = Platform(projects / "zer0-cms", projects, out, port)
        self.state: dict[str, Any] = {}
        self.stages: list[dict[str, Any]] = []

    def repo(self, name: str) -> Path:
        return self.projects / name

    def station(self, name: str) -> Station:
        return Station(self.platform.belt_root, name)

    def adapter(self, script: str, *args: Any, env: dict[str, str] | None = None, timeout: int = 2400) -> dict[str, Any]:
        logs = self.out / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([str(HERE / "adapters" / script), *map(str, args)], capture_output=True, text=True,
                              timeout=timeout, env={**os.environ, **(env or {})})
        (logs / f"{script}.{int(time.time())}.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip().startswith("{")]
        summary = json.loads(lines[-1]) if lines else {"ok": False, "error": proc.stderr[-800:]}
        summary["_exit"] = proc.returncode
        return summary

    def docker(self, *args: str, timeout: int = 1800, log: str | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(["docker", "run", "--rm", *args], capture_output=True, text=True, timeout=timeout)
        if log:
            logs = self.out / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / f"{log}.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        return proc


# --------------------------------------------------------------------------- content


def wire_story() -> dict[str, Any]:
    return json.loads((HERE / "fixtures" / "wire_story.json").read_text())


def template_body(name: str, values: dict[str, Any]) -> str:
    """The Markdown body of one of the e2e post templates (its front matter is written field by field instead)."""
    text = Template((HERE / "fixtures" / "posts" / name).read_text()).safe_substitute(values)
    return text.split("\n---\n", 1)[1].lstrip("\n")


def wire_values(claims: dict[str, dict[str, Any]], revision: int) -> dict[str, Any]:
    urls = {key: src["url"] for key, src in wire_story()["sources"].items()}
    return {"date": TODAY.isoformat(), "revision": revision, "context": claims["q7-context"]["answer"],
            "license": claims["q7-license"]["answer"], "weights_date": claims["q7-weights-date"]["answer"],
            "url_model_card": urls["model-card"], "url_leaderboard": urls["leaderboard"],
            "url_vendor_blog": urls["vendor-blog"]}


def wire_front_matter(claims: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "excerpt": "The model card, the leaderboard and the vendor blog agree that Q-7 exists. After that, the desk is on its own.",
        "permalink": "/wire/quokka-q7-e2e/",
        "sources": [src["url"] for src in wire_story()["sources"].values()],
        "claims": [{k: c[k] for k in ("id", "question", "answer", "confidence", "sources")} for c in claims],
        "e2e_fixture": True,
    }


def hack_front_matter() -> dict[str, Any]:
    return {
        "excerpt": "lsof and ss both find the process holding a port. Both also show an empty result for a port they are not allowed to see.",
        "permalink": "/hacks/find-the-process-on-port-8080/",
        "e2e_fixture": True,
    }


# --------------------------------------------------------------------------- stages


def stage_preflight(run: PlatformRun) -> dict[str, Any]:
    repos = {}
    for name in REPOS:
        path = run.repo(name)
        branch = subprocess.run(["git", "-C", str(path), "branch", "--show-current"], capture_output=True, text=True)
        repos[name] = {"branch": branch.stdout.strip(), **repo_state(path)}
    docker = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
    lh_dirty = [p for p in repos["lifehacker.dev"]["uncommitted"] if not p.startswith("??")]
    return {"status": "pass" if docker.returncode == 0 and not lh_dirty else "fail", "repos": repos,
            "docker": docker.stdout.strip(), "lifehacker_tracked_changes": lh_dirty}


def stage_platform_up(run: PlatformRun) -> dict[str, Any]:
    p = run.platform
    started = time.time()
    if not run.resume:
        p.write_override()
        # The platform's services as zer0-cms's AGENTS.md starts them (not the retired Rails `cms`, which has no profile).
        p.compose("up", "-d", "--build", "--wait", "--wait-timeout", "600", *SERVICES, log="compose-up")
    raw = p.compose("ps", "--format", "json").stdout.strip()
    rows = json.loads(raw) if raw.startswith("[") else [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
    services = {s.get("Service"): s.get("State") for s in rows}
    # The person: a superuser holds the desk's permissions (ADR 0007 leaves the groups to the owner). The password
    # lives only in this process.
    password = secrets.token_urlsafe(18)
    username = f"e2e-editor-{NOW:%H%M%S}"
    p.manage("createsuperuser", "--noinput", env={"DJANGO_SUPERUSER_USERNAME": username,
             "DJANGO_SUPERUSER_PASSWORD": password, "DJANGO_SUPERUSER_EMAIL": f"{username}@example.invalid"})
    me = p.sign_in(username, password)
    belt = p.api("GET", "outbox/belt/")
    want = set(SERVICES)
    return {
        "status": "pass" if want <= {k for k, v in services.items() if v == "running"} and belt["harness"] else "fail",
        "services": services, "seconds_to_ready": round(time.time() - started, 1),
        "signed_in_as": me.get("username") if isinstance(me, dict) else me, "belt": belt,
    }


def stage_platform_import(run: PlatformRun) -> dict[str, Any]:
    p = run.platform
    out = "resumed: the site was imported by the run that started the stack" if run.resume else \
        p.manage("import_jekyll", "--site", SITE_KEY, "--path", "/sites/lifehacker.dev", log="import")
    sites = p.api("GET", "sites/")
    site = next(s for s in (sites if isinstance(sites, list) else sites.get("results", [])) if s["hostname"] == SITE_HOST)
    children = p.api("GET", "content/pages/", params={"parent": site["root_page_id"], "page_size": 100})
    rows = children if isinstance(children, list) else children.get("results", [])
    posts = None
    for row in rows:
        detail = p.api("GET", f"content/pages/{row['id']}/")
        if detail["fields"].get("collection") == "posts":
            posts = detail
            break
    if posts is None:
        raise RuntimeError("the imported site has no posts collection")
    types = p.api("GET", f"content/pages/{posts['id']}/types/")
    run.state.update(site=site, posts_id=posts["id"])
    return {"status": "pass" if any(t["type"] == "cms.ArticlePage" for t in types) else "fail",
            "import_report_tail": out.strip().splitlines()[-6:], "site": site, "posts_collection": posts["id"],
            "creatable_types": [t["type"] for t in types]}


def stage_sense(run: PlatformRun) -> dict[str, Any]:
    p = run.platform
    hack_idea = json.loads((HERE / "fixtures" / "ideas.json").read_text())["scout-a"][0]
    story = wire_story()
    wire_idea = {**story["idea"], "collection": "wire"}
    sent = {}
    for key, idea in (("hack", hack_idea), ("wire", wire_idea)):
        sent[key] = p.api("POST", "outbox/belt/send/", {
            "kind": "idea", "to": ["planner", "ledger"], "subject": f"{key}:{idea['title'][:80]}", "payload": idea,
        }, expect=(201,))
    summary = p.deliver()
    run.state["ideas"] = {k: v["id"] for k, v in sent.items()}
    run.state["traces"] = {k: v["trace"] for k, v in sent.items()}
    return {"status": "pass" if summary["delivered"] >= 2 and summary["dead_letters"] == 0 else "fail",
            "sent_from": sorted({v["from"] for v in sent.values()}), "ideas": run.state["ideas"],
            "traces": run.state["traces"], "belt": summary}


def stage_plan(run: PlatformRun) -> dict[str, Any]:
    lh = run.repo("lifehacker.dev")
    branch = f"e2e/platform-run-{NOW:%Y%m%d-%H%M}"
    subprocess.run(["git", "-C", str(lh), "switch", "-c", branch], check=True, capture_output=True, text=True)
    run.state["lh_branch"] = branch
    ideas = run.station("planner").receive(kinds=("idea",))
    by_trace = {m.trace: m for m in ideas}
    hack, wire = by_trace[run.state["traces"]["hack"]], by_trace[run.state["traces"]["wire"]]
    work = run.out / "plan"
    work.mkdir(parents=True, exist_ok=True)
    scout_row = {k: hack.payload[k] for k in ("collection", "title", "brief", "voice", "source_url", "source_title", "rationale")}
    wire_row = {"title": wire.payload["title"], "brief": wire.payload["brief"],
                "source_url": wire_story()["sources"]["model-card"]["url"], "source_title": "Q-7 model card (fixture)",
                "source_id": "e2e-fixture", "trust": "primary", "rationale": "Sent from the zer0-cms Outbox desk (e2e)."}
    (work / "scout.jsonl").write_text(json.dumps(scout_row) + "\n")
    (work / "wire.jsonl").write_text(json.dumps(wire_row) + "\n")
    # The planner is the single writer: one serialized run of each of lifehacker.dev's builders over the belt's ideas.
    # The ideas themselves stay on the belt's ledger, so both proposal files are put back as they were.
    script = (
        "set -e; cd /site; cp _data/scout/ideas.jsonl /tmp/scout.orig; cp /in/scout.jsonl _data/scout/ideas.jsonl;"
        " ruby scripts/scout/build_backlog.rb --apply --max-add 1 > /out/scout.txt 2>&1;"
        " cp /tmp/scout.orig _data/scout/ideas.jsonl;"
        " cp /in/wire.jsonl _data/wire/ideas.jsonl; ruby scripts/wire/build_backlog.rb --apply --max-add 1 > /out/wire.txt 2>&1;"
        " rm -f _data/wire/ideas.jsonl;"
        " ruby scripts/ci/lint_artifacts.rb > /out/lint.txt 2>&1; echo lint_rc=$?"
    )
    proc = run.docker("--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{lh}:/site",
                      "-v", f"{work}:/in:ro", "-v", f"{work}:/out", "ruby:3.4", "bash", "-c", script, log="plan")
    scout_txt = (work / "scout.txt").read_text() if (work / "scout.txt").exists() else ""
    wire_txt = (work / "wire.txt").read_text() if (work / "wire.txt").exists() else ""
    src = re.search(r"\+ (SRC-\d+)", scout_txt)
    wid = re.search(r"\+ (WIRE-\d+)", wire_txt)
    lint_rc = int(re.search(r"lint_rc=(\d+)", proc.stdout).group(1)) if "lint_rc=" in proc.stdout else None
    if not (src and wid):
        raise RuntimeError(f"no backlog id minted: scout={scout_txt[-400:]!r} wire={wire_txt[-400:]!r} {proc.stderr[-400:]}")
    ids = {"hack": src.group(1), "wire": wid.group(1)}
    assignments = {}
    for key, msg in (("hack", hack), ("wire", wire)):
        a = msg.reply("assignment", "planner", ["drafter", "ledger"],
                      {"backlog_id": ids[key], "kind": "hack" if key == "hack" else "wire", "title": msg.payload["title"]})
        run.station("planner").send(a)
        assignments[key] = a.id
    summary = run.platform.deliver()
    diff = subprocess.run(["git", "-C", str(lh), "diff", "--stat"], capture_output=True, text=True).stdout
    run.state.update(backlog_ids=ids, assignments=assignments)
    return {"status": "pass" if lint_rc == 0 and summary["dead_letters"] == 0 else "fail", "lifehacker_branch": branch,
            "backlog_ids": ids, "lint_artifacts_exit": lint_rc, "lifehacker_diff": diff.strip().splitlines()[-3:],
            "belt": summary}


def create_page(run: PlatformRun, *, title: str, slug: str, description: str, author: str, tags: list[str],
                category: str, path: str, front_matter: dict[str, Any], body: str) -> dict[str, Any]:
    p = run.platform
    categories = run.state["categories"]
    return p.api("POST", "content/pages/", {
        "parent": run.state["posts_id"], "type": "cms.ArticlePage",
        "fields": {"title": title, "slug": slug, "search_description": description, "author": author,
                   "date": f"{TODAY.isoformat()}T00:00:00Z", "tags": tags, "categories": [categories[category]],
                   FRONT_MATTER_FIELD: front_matter, PATH_FIELD: path},
        "body": [{"type": "markdown", "value": body}],
    }, expect=(201,))


def stage_produce(run: PlatformRun) -> dict[str, Any]:
    p = run.platform
    drafts = run.station("drafter").receive(kinds=("assignment",))
    by_trace = {m.trace: m for m in drafts}
    # The categories' ids: an imported post's field specs list every category as a choice.
    listed = p.api("GET", "content/pages/", params={"parent": run.state["posts_id"], "page_size": 1})
    sample = p.api("GET", f"content/pages/{(listed if isinstance(listed, list) else listed['results'])[0]['id']}/")
    spec = next(s for s in sample["field_specs"] if s["name"] == "categories")
    run.state["categories"] = {c["label"]: c["value"] for c in spec["choices"]}
    story = wire_story()
    claims = {c["id"]: dict(c) for c in story["claims"]}
    run.state["claims"] = claims
    # The drafter writes what the sources said at filing time: the leaderboard still listed Q-7 as proprietary.
    claims["q7-license"]["answer"] = "proprietary"
    claims["q7-weights-date"]["answer"] = "November 4"
    hack_page = create_page(
        run, title="Find the process squatting on port 8080: lsof, ss, and the flag everyone forgets",
        slug="find-the-process-on-port-8080",
        description="Address already in use, and nothing says by whom. Two commands answer it, and each has one flag that decides whether the answer is true.",
        author="edge", tags=["docker", "shell"], category="Hacks", path=HACK_PATH, front_matter=hack_front_matter(),
        body=template_body("hack.md", {"date": TODAY.isoformat(), "title": ""}))
    wire_page = create_page(
        run, title=story["idea"]["title"], slug="quokka-q7-e2e",
        description="Three sources, three answers: the model card, the leaderboard and the vendor blog each describe Q-7 differently. The desk reports all three.",
        author="rhea", tags=["ai", "news"], category="The Wire", path=WIRE_PATH,
        front_matter=wire_front_matter(list(claims.values())), body=template_body("wire.md", wire_values(claims, 1)))
    pages = {"hack": hack_page, "wire": wire_page}
    sent = {}
    for key, page in pages.items():
        assignment = by_trace[run.state["traces"][key]]
        d = assignment.reply("draft", "drafter", ["verifier", "ledger"],
                             {"backlog_id": assignment.payload["backlog_id"], "kind": assignment.payload["kind"], "revision": 1},
                             ref={"page_id": page["id"], "path": page["fields"].get(PATH_FIELD), "site": SITE_KEY})
        run.station("drafter").send(d)
        sent[key] = d.id
    summary = p.deliver()
    run.state.update(pages={k: v["id"] for k, v in pages.items()}, drafts=sent)
    ok = all(pg["fields"].get(PATH_FIELD) == path for pg, path in ((hack_page, HACK_PATH), (wire_page, WIRE_PATH)))
    return {"status": "pass" if ok and summary["dead_letters"] == 0 else "fail",
            "pages": {k: {"id": v["id"], "status": v.get("status"), "path": v["fields"].get(PATH_FIELD),
                          "revision": (v.get("latest_revision") or {}).get("id")} for k, v in pages.items()},
            "categories_seen": sorted(run.state["categories"])[:12], "belt": summary}


def claim_check(run: PlatformRun, title: str, claims: dict[str, dict[str, Any]], *, closes_at_tick: int = 8) -> dict[str, Any]:
    p = run.platform
    started = p.api("POST", "outbox/claim-checks/", {
        "title": title, "author": "rhea", "page": run.state["pages"]["wire"],
        "claims": [{k: c[k] for k in ("id", "question", "answer", "confidence", "sources")} for c in claims.values()],
        "timeline": [{k: row[k] for k in ("tick", "source", "says") if k in row} | ({"fact": row["fact"]} if row.get("fact") else {})
                     for row in wire_story()["timeline"]],
        "closes_at_tick": closes_at_tick, "lag_b": 2,
    }, expect=(202,))
    return p.wait_run("claim-checks", started["id"])


def verdicts(check: dict[str, Any]) -> list[dict[str, Any]]:
    report = check.get("report") or {}
    return [{k: c.get(k) for k in ("claim_id", "verdict", "reason", "draft_answer", "supported_answer")}
            for c in report.get("claims", [])]


def export_site(run: PlatformRun, name: str, *, drafts: bool) -> Path:
    args = ["export_jekyll", "--site", SITE_KEY, "--out", f"/export/{name}"] + (["--include-drafts"] if drafts else [])
    run.platform.manage(*args, log=f"export-{name}")
    return run.platform.export_dir / name


def site_copy_with(run: PlatformRun, name: str, export: Path, rels: list[str]) -> Path:
    """lifehacker.dev's working tree, copied, with the platform's exported files for `rels` laid over it."""
    site = run.out / "sites" / name
    if site.exists():
        shutil.rmtree(site)
    copy_working_tree(run.repo("lifehacker.dev"), site)
    for rel in rels:
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(export / rel, site / rel)
    return site


def gates(run: PlatformRun, site: Path, name: str, files: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    out = run.out / "gates" / name
    summary = run.adapter("lifehacker_gates.sh", site, out, *files, env=env)
    findings = []
    path = Path(summary.get("findings_jsonl") or out / "test-results" / "findings.jsonl")
    if path.exists():
        for line in path.read_text().splitlines():
            row = json.loads(line) if line.strip() else {}
            if any(f in str(row.get("file") or "") for f in files):
                findings.append({k: row.get(k) for k in ("check_id", "severity", "rule", "file", "evidence")})
    blocking = [f for f in findings if f["severity"] == "error"]
    return {"ok": bool(summary.get("ok")) and not blocking, "adapter": {k: summary.get(k) for k in
            ("ok", "scoped", "checks_run", "checks_skipped", "by_severity", "_exit")}, "new_file_findings": findings}


def stage_verify(run: PlatformRun) -> dict[str, Any]:
    p = run.platform
    inbox = {m.trace: m for m in run.station("verifier").receive(kinds=("draft",))}
    hack_draft, wire_draft = inbox[run.state["traces"]["hack"]], inbox[run.state["traces"]["wire"]]
    claims = run.state["claims"]
    first = claim_check(run, "Q-7 dispatch, revision 1", claims)
    block = wire_draft.reply("verdict", "verifier", ["drafter", "editor", "ledger"],
                             {"verdict": "pass" if first.get("passed") else "block", "claim_check": first["id"],
                              "desk": verdicts(first)})
    run.station("verifier").send(block)
    p.deliver()
    # The drafter takes the block back to the platform: every claim the sources no longer support is corrected there.
    corrected = []
    for v in verdicts(first):
        if v["verdict"] == "block" and v["supported_answer"] is not None:
            claims[v["claim_id"]]["answer"] = v["supported_answer"]
            claims[v["claim_id"]]["confidence"] = 0.85
            corrected.append(v["claim_id"])
    run.station("drafter").receive(kinds=("verdict",))
    page = p.api("GET", f"content/pages/{run.state['pages']['wire']}/")
    saved = p.api("PATCH", f"content/pages/{page['id']}/", {
        "base_revision_id": (page.get("latest_revision") or {}).get("id"),
        "fields": {FRONT_MATTER_FIELD: wire_front_matter(list(claims.values()))},
        "body": [{"type": "markdown", "value": template_body("wire.md", wire_values(claims, 2))}],
    })
    redraft = block.reply("draft", "drafter", ["verifier", "ledger"], {"revision": 2, "corrected": corrected},
                          ref={"page_id": page["id"], "revision": (saved.get("latest_revision") or {}).get("id")})
    run.station("drafter").send(redraft)
    p.deliver()
    run.station("verifier").receive(kinds=("draft",))
    second = claim_check(run, "Q-7 dispatch, revision 2", claims)
    run.state["claim_check"] = second["id"]
    # lifehacker.dev's own gates over the platform's exported drafts, scoped the way `verify` scopes a pull request.
    drafts = export_site(run, "drafts", drafts=True)
    site = site_copy_with(run, "drafts", drafts, [HACK_PATH, WIRE_PATH])
    gate = gates(run, site, "drafts", [HACK_PATH, WIRE_PATH])
    passes = {
        "hack": gate["ok"],
        "wire": gate["ok"] and bool(second.get("passed")),
    }
    hack_verdict = hack_draft.reply("verdict", "verifier", ["publisher", "ledger"],
                                    {"verdict": "pass" if passes["hack"] else "block", "tier": "low-risk"})
    wire_verdict = redraft.reply("verdict", "verifier", ["editor", "ledger"],
                                 {"verdict": "pass" if passes["wire"] else "block", "tier": "factual",
                                  "claim_check": second["id"], "desk": verdicts(second)})
    run.station("verifier").send(hack_verdict)
    run.station("verifier").send(wire_verdict)
    p.deliver()
    approval = None
    if passes["wire"]:
        # A factual piece waits for a person: the editor approves it from the platform's Outbox desk.
        approval = p.api("POST", "outbox/belt/send/", {
            "kind": "approval", "to": ["publisher", "ledger"], "parent_id": wire_verdict.id,
            "payload": {"note": "Claims re-checked against the sources; approved for publication."},
            "page": run.state["pages"]["wire"],
        }, expect=(201,))
        p.deliver()
    return {
        "status": "pass" if not first.get("passed") and second.get("passed") and gate["ok"] and approval else "fail",
        "claim_desk_rounds": [{"round": 1, "passed": first.get("passed"), "claims": verdicts(first)},
                              {"round": 2, "passed": second.get("passed"), "claims": verdicts(second)}],
        "corrected_in_platform": corrected, "draft_saved_revision": (saved.get("latest_revision") or {}).get("id"),
        "lifehacker_gates_on_exported_drafts": gate, "approval": approval and {k: approval[k] for k in ("id", "from", "kind")},
    }


def stage_publish(run: PlatformRun) -> dict[str, Any]:
    p = run.platform
    published = {k: p.api("POST", f"content/pages/{pid}/publish/") for k, pid in run.state["pages"].items()}
    page_ids = set(run.state["pages"].values())

    def announced(_: Any) -> bool:
        p.api("POST", "outbox/belt/deliver/")
        rows = p.api("GET", "outbox/belt/messages/", params={"kind": "published", "page_size": 50})
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        run.state["published_msgs"] = [r for r in rows if (r.get("ref") or {}).get("page_id") in page_ids]
        return len(run.state["published_msgs"]) == len(page_ids)

    p.wait("outbox/belt/", announced, timeout=240, every=5)
    belt = p.deliver()
    learner = run.station("learner").receive(kinds=("published",))
    # The platform's export becomes lifehacker.dev's files: the run's two new posts, onto the run's branch.
    live = export_site(run, "live", drafts=False)
    lh = run.repo("lifehacker.dev")
    compare = {"identical": 0, "differs": [], "new": []}
    for f in sorted(x for x in live.rglob("*") if x.is_file()):
        rel = str(f.relative_to(live))
        target = lh / rel
        if not target.exists():
            compare["new"].append(rel)
        elif target.read_bytes() == f.read_bytes():
            compare["identical"] += 1
        else:
            compare["differs"].append(rel)
    for rel in (HACK_PATH, WIRE_PATH):
        (lh / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live / rel, lh / rel)
    # lifehacker.dev's own cover-art renderer (offline, deterministic) stamps `preview:` into each post.
    art = {}
    for rel in (HACK_PATH, WIRE_PATH):
        proc = run.docker("--network=none", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                          "-v", f"{lh}:/repo", "-w", "/repo", "node:22-slim", "node", "scripts/preview/generate.mjs",
                          "-f", rel, log=f"preview-{Path(rel).stem}")
        art[rel] = {"exit": proc.returncode, "stamped": "preview:" in (lh / rel).read_text()}
    site = run.out / "sites" / "output"
    if site.exists():
        shutil.rmtree(site)
    copy_working_tree(lh, site)
    changed = sorted({*(HACK_PATH, WIRE_PATH), *[ln[3:] for ln in subprocess.run(
        ["git", "-C", str(lh), "status", "--porcelain", "-uall"], capture_output=True, text=True).stdout.splitlines()]})
    changed_files = [c for c in changed if (lh / c).is_file()]
    gate_first = gates(run, site, "output", changed_files)
    build = run.adapter("lifehacker_build.sh", site, run.out / "build", HACK_PATH, WIRE_PATH)
    site_dir = Path(build.get("site_dir") or run.out / "build" / "_site")
    gate_built = gates(run, site, "output-built", changed_files,
                       env={"LH_SITE_DIR": str(site_dir), "LH_BUILD_RC": "0" if build.get("ok") else "1"})
    rendered = build.get("posts") or []
    ok = (len(run.state["published_msgs"]) == 2 and set(compare["new"]) >= {HACK_PATH, WIRE_PATH}
          and all(a["stamped"] for a in art.values()) and gate_first["ok"] and build.get("ok") and gate_built["ok"]
          and all(r.get("exists") and r.get("title_found") for r in rendered))
    return {
        "status": "pass" if ok else "fail",
        "published": {k: {"live": v.get("live"), "live_revision_id": v.get("live_revision_id")} for k, v in published.items()},
        "belt_published": [{k: m.get(k) for k in ("seq", "id", "from", "to", "ref", "parent_id")} for m in run.state["published_msgs"]],
        "learner_received": len(learner), "belt": belt,
        "export_vs_lifehacker": {"identical": compare["identical"], "differs": compare["differs"][:20],
                                 "differs_count": len(compare["differs"]), "new": compare["new"]},
        "cover_art": art, "lifehacker_changed": changed, "gates": gate_first,
        "build": {k: build.get(k) for k in ("ok", "pages", "posts", "_exit")}, "gates_with_built_site": gate_built,
    }


def stage_learn(run: PlatformRun) -> dict[str, Any]:
    p = run.platform
    story = wire_story()
    p.api("POST", f"outbox/claim-checks/{run.state['claim_check']}/resolve/", {"truth": story["truth"]}, expect=(202,))
    resolved = p.wait(f"outbox/claim-checks/{run.state['claim_check']}/",
                      lambda r: r.get("resolution_status") in ("succeeded", "failed"), timeout=300)
    calibration = p.api("GET", "outbox/claim-checks/calibration/")
    # A later read of the sources: the vendor moved the weights date after the dispatch went live.
    recheck = claim_check(run, "Q-7 dispatch, published: recheck", run.state["claims"], closes_at_tick=28)
    blocked = [v for v in verdicts(recheck) if v["verdict"] == "block"]
    signal = None
    if blocked:
        wire_published = next(m for m in run.state["published_msgs"] if m["ref"]["page_id"] == run.state["pages"]["wire"])
        signal = p.api("POST", "outbox/belt/send/", {
            "kind": "signal", "to": ["planner", "ledger"], "parent_id": wire_published["id"],
            "trace": run.state["traces"]["wire"],
            "payload": {"reason": "stale-published-claim", "claims": blocked, "claim_check": recheck["id"]},
            "page": run.state["pages"]["wire"],
        }, expect=(201,))
        p.deliver()
    return {
        "status": "pass" if resolved.get("resolution_status") == "succeeded" and blocked and signal else "fail",
        "resolutions": resolved.get("resolutions"), "calibration": calibration,
        "recheck": {"passed": recheck.get("passed"), "blocked": blocked},
        "correction_signal": signal and {k: signal[k] for k in ("id", "from", "kind", "to")},
    }


def stage_orchestrate(run: PlatformRun) -> dict[str, Any]:
    results = {}
    blueprints = sorted((HERE / "blueprints").glob("*.blueprint.json"))
    lh = run.repo("lifehacker.dev") / ".factory" / "issue-factory.blueprint.json"
    for bp in [*blueprints, lh]:
        name = bp.name if bp.parent == HERE / "blueprints" else f"lifehacker.dev:{bp.name}"
        out = run.out / "gitorio" / name.replace(":", "-")
        summary = run.adapter("gitorio_compile.sh", bp, out, env={"GITORIO_REPO": str(run.repo("gitorio"))})
        results[name] = {k: summary.get(k) for k in ("ok", "workflows", "errors", "guards")}
        if '"trigger.repository_dispatch"' in bp.read_text():
            results[name]["payload_checked"] = any("dispatch.payload" in y.read_text() for y in out.glob("*.yml"))
    ok = all(r["ok"] for r in results.values()) and all(r.get("payload_checked", True) for r in results.values())
    return {"status": "pass" if ok else "fail", "compiles": results}


def stage_toolbelt(run: PlatformRun) -> dict[str, Any]:
    scripts = run.repo("scripts")
    smoke = run.docker("--network=none", "-v", f"{scripts}:/src:ro", "ruby:3.4", "bash", "-c",
                       "cp -r /src /tmp/scripts && cd /tmp/scripts && bash tests/smoke.sh", log="scripts-smoke")
    lint = run.docker("-v", f"{scripts}:/mnt:ro", "-w", "/mnt", "koalaman/shellcheck:stable", "--severity=warning",
                      "project-init.sh", "install.sh", "git_init.sh", "rename-directory.sh", "create_package.sh",
                      "FORKME/forkme.sh", "STASHME/stashme.sh", "tests/smoke.sh",
                      log="scripts-shellcheck")
    oks = [ln for ln in smoke.stdout.splitlines() if ln.startswith("ok")]
    fails = [ln for ln in smoke.stdout.splitlines() if ln.startswith("FAIL")]
    return {"status": "pass" if smoke.returncode == 0 and lint.returncode == 0 else "fail",
            "smoke": {"exit": smoke.returncode, "ok": len(oks), "fail": fails}, "shellcheck_exit": lint.returncode}


def stage_harness(run: PlatformRun) -> dict[str, Any]:
    outbox = HERE.parent
    proc = run.docker("--network=none", "-v", f"{outbox}:/src:ro", "python:3.13-slim", "sh", "-c",
                      "cp -r /src /tmp/o && cd /tmp/o && PYTHONPATH=src python -m unittest discover -s tests 2>&1 | tail -3"
                      " && PYTHONPATH=src python -m outbox.cli suite --tasks tasks --out /tmp/suite > /tmp/suite.json"
                      " && echo suite_ok", log="outbox-harness")
    ran = re.search(r"Ran (\d+) tests", proc.stdout)
    return {"status": "pass" if proc.returncode == 0 and "\nOK" in proc.stdout and "suite_ok" in proc.stdout else "fail",
            "tests": int(ran.group(1)) if ran else None, "suite": "suite_ok" in proc.stdout}


def stage_ledger(run: PlatformRun) -> dict[str, Any]:
    p = run.platform
    summary = p.api("GET", "outbox/belt/")
    journeys = {}
    for key, trace in run.state["traces"].items():
        rows = p.api("GET", f"outbox/belt/traces/{trace}/")["messages"]
        journeys[key] = [f"{m['seq']}:{m['from']}:{m['kind']}" for m in rows]
    need = {"hack": ["idea", "assignment", "draft", "verdict", "published"],
            "wire": ["idea", "assignment", "draft", "verdict", "draft", "verdict", "approval", "published", "signal"]}
    complete = {k: [s.split(":")[2] for s in v] == need[k] for k, v in journeys.items()}
    ok = summary["seq_gapless"] and summary["ids_unique"] and summary["dead_letters"] == 0 and all(complete.values())
    return {"status": "pass" if ok else "fail", "belt": summary, "journeys": journeys, "complete": complete}


STAGES: list[tuple[str, str, Callable[[PlatformRun], dict[str, Any]], tuple[str, ...]]] = [
    ("preflight", "Repos at their merged mains; Docker", stage_preflight, ()),
    ("platform-up", "zer0-cms runs as its Compose stack; a person signs in", stage_platform_up, ("preflight",)),
    ("platform-import", "zer0-cms imports lifehacker.dev", stage_platform_import, ("platform-up",)),
    ("sense", "Ideas sent onto the belt from the platform's Outbox desk", stage_sense, ("platform-import",)),
    ("plan", "lifehacker.dev's backlog builders mint the ids; the planner assigns them", stage_plan, ("sense",)),
    ("produce", "The person writes both pieces as draft pages in the platform", stage_produce, ("plan",)),
    ("verify", "Claim desk in the platform's worker; correction; lifehacker.dev gates; approval", stage_verify, ("produce",)),
    ("publish", "Publish in the platform; the belt announces it; export to lifehacker.dev; cover art; gates; build",
     stage_publish, ("verify",)),
    ("learn", "Resolve and calibrate in the platform; a recheck signals a correction", stage_learn, ("publish",)),
    ("orchestrate", "gitorio compiles the pipeline's lines", stage_orchestrate, ()),
    ("toolbelt", "scripts' safety smoke and shellcheck", stage_toolbelt, ()),
    ("harness", "outbox's own suite", stage_harness, ()),
    ("ledger", "One gapless belt traces each piece", stage_ledger, ("learn",)),
]


def teardown(run: PlatformRun) -> dict[str, Any]:
    p = run.platform
    p.compose("logs", "--no-color", "--timestamps", check=False, log="compose-logs", timeout=300)
    if run.keep_up:
        return {"left_running": True, "url": p.base}
    down = p.compose("down", "-v", "--remove-orphans", check=False, log="compose-down", timeout=600)
    return {"left_running": False, "down_exit": down.returncode}


def write_report(run: PlatformRun, extra: dict[str, Any]) -> None:
    counts: dict[str, int] = {}
    for s in run.stages:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "counts": counts,
              "lifehacker_branch": run.state.get("lh_branch"), "stages": run.stages, **extra}
    (run.out / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    lines = ["# Platform run: zer0-cms to lifehacker.dev", "",
             f"> Generated {report['generated_at']}; {counts}; output on lifehacker.dev branch `{report['lifehacker_branch']}`.", "",
             "| Stage | Result | Seconds | What it shows |", "| --- | --- | --- | --- |"]
    lines += [f"| {s['name']} | {s['status']} | {s['seconds']} | {s['title']} |" for s in run.stages]
    (run.out / "report.md").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--projects", type=Path, default=HERE.parent.parent, help="where the sibling repos live")
    parser.add_argument("--out", type=Path, default=HERE.parent / "runs" / f"platform-run-{NOW:%Y%m%d-%H%M%S}")
    parser.add_argument("--port", type=int, default=18000, help="the platform's port on 127.0.0.1")
    parser.add_argument("--keep-up", action="store_true", help="leave the zer0-cms stack running afterwards")
    parser.add_argument("--only", nargs="*", default=[], help="run only these stages (the rest are reported as skipped)")
    parser.add_argument("--resume", action="store_true",
                        help="reuse the stack a --keep-up run left up (same --out): no build, no re-import")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    run = PlatformRun(args.projects.resolve(), out, args.port, args.keep_up or args.resume, args.resume)
    failed: set[str] = set()
    try:
        for name, title, fn, needs in STAGES:
            started = time.time()
            print(f"[platform-run] {name}: {title} ...", flush=True)
            if args.only and name not in args.only:
                result = {"status": "skip", "reason": "not selected (--only)"}
            elif blocked := [n for n in needs if n in failed]:
                result = {"status": "skip", "reason": f"needs {', '.join(blocked)}"}
            else:
                try:
                    result = fn(run)
                except Exception as e:  # a stage's failure is its result; later stages that need it are skipped
                    result = {"status": "fail", "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-3000:]}
            if result["status"] == "fail" or (result["status"] == "skip" and not args.only):
                failed.add(name)
            run.stages.append({"name": name, "title": title, "seconds": round(time.time() - started, 1), **result})
            print(f"[platform-run] {name}: {result['status']} ({run.stages[-1]['seconds']}s)"
                  + (f" — {result.get('error') or result.get('reason')}" if result["status"] != "pass" else ""), flush=True)
    finally:
        down = teardown(run) if (run.platform.override.exists()) else {}
        write_report(run, {"teardown": down, "api_calls": len(run.platform.calls)})
    print(json.dumps({s["name"]: s["status"] for s in run.stages}))
    print(f"report: {out / 'report.md'}")
    return 0 if all(s["status"] == "pass" for s in run.stages) else 1


if __name__ == "__main__":
    raise SystemExit(main())
