#!/usr/bin/env bash
# zer0cms_roundtrip.sh LIFEHACKER_SNAPSHOT_DIR OUT_DIR
#
# Round-trips a Jekyll site snapshot (lifehacker.dev) through the zer0-cms
# platform (Django + Wagtail) in a throwaway container and reports, file by
# file, whether the export is byte-identical to the snapshot. Optionally proves
# that the import's publishes land in the transactional outbox as
# `content.page.published` CloudEvents and relay to the Redis stream
# `zer0:content`.
#
# What runs, inside the platform image (zer0-cms-content:latest by default),
# with a git-archive snapshot of zer0-cms HEAD's content/ and schemas/ mounted
# read-only over the image's code so the run uses HEAD:
#   manage.py migrate                                  (throwaway DB in OUT_DIR)
#   manage.py roundtrip_jekyll --path /site --site <key>-rt   (the platform's own verdict; rolled back)
#   manage.py import_jekyll --site <key> --path /site
#   manage.py export_jekyll --site <key> --out OUT_DIR/export --include-drafts
#   manage.py relay_outbox --once --batch 500  (looped)       (EVENTS=1: to a throwaway Redis)
# then compares every Jekyll content file of the snapshot with the export.
#
# Never writes to the snapshot (mounted :ro) or to either checkout; refuses an
# OUT_DIR inside them. Containers/network it creates are named e2e-zer0rt-*
# and removed on exit.
#
# Env knobs:
#   ZER0CMS_IMAGE        platform image                 (zer0-cms-content:latest)
#   ZER0CMS_REPO         checkout to snapshot code from  (the sibling ../zer0-cms)
#   ZER0CMS_SRC          an existing dir holding content/ + schemas/ to mount instead
#   ZER0CMS_IMAGE_CODE=1 use the image's own code (no mount)
#   LIFEHACKER_REPO      second protected checkout       (the sibling ../lifehacker.dev)
#   SITE_KEY             platform site key               (lifehacker)
#   DB=sqlite|postgres   database (sqlite file in OUT_DIR, or a throwaway pgvector container)
#   EVENTS=1|0           count outbox events + relay them to a throwaway Redis (0 → "events": null)
#   OFFICIAL=1|0         also run manage.py roundtrip_jekyll
#
# Stdout: progress goes to stderr; the LAST stdout line is the JSON summary.
# Exit: 0 when ok, 1 when the round trip is not byte-identical, 2 on a usage
# error, other non-zero when a step failed (a fallback JSON is still printed).

set -euo pipefail

usage() { echo "usage: $0 LIFEHACKER_SNAPSHOT_DIR OUT_DIR" >&2; exit 2; }
[ $# -eq 2 ] || usage

now() { perl -MTime::HiRes=time -e 'printf "%.3f\n", time' 2>/dev/null || date +%s; }
log() { echo "[zer0cms_roundtrip] $*" >&2; }
T0=$(now)

[ -d "$1" ] || { echo "snapshot dir not found: $1" >&2; exit 2; }
SNAP=$(cd "$1" && pwd -P)
[ -f "$SNAP/_config.yml" ] || { echo "$SNAP has no _config.yml — not a Jekyll site" >&2; exit 2; }
mkdir -p "$2"
OUT=$(cd "$2" && pwd -P)

IMAGE=${ZER0CMS_IMAGE:-zer0-cms-content:latest}
ZREPO=${ZER0CMS_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../zer0-cms" && pwd)}
LREPO=${LIFEHACKER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../lifehacker.dev" && pwd)}
SITE_KEY=${SITE_KEY:-lifehacker}
DB=${DB:-sqlite}
EVENTS=${EVENTS:-1}
OFFICIAL=${OFFICIAL:-1}

# ── guards: never write inside the protected checkouts ─────────────────────
for p in "$ZREPO" "$LREPO"; do
  [ -d "$p" ] || continue
  rp=$(cd "$p" && pwd -P)
  case "$OUT/" in "$rp"/*) echo "refusing OUT_DIR inside $rp" >&2; exit 2 ;; esac
  case "$SNAP/" in "$rp"/*) log "warning: snapshot is the live checkout $rp (mounted read-only; prefer a git archive snapshot)" ;; esac
done
case "$SNAP/" in "$OUT"/*) echo "OUT_DIR must not contain the snapshot" >&2; exit 2 ;; esac
case "$OUT/" in "$SNAP"/*) echo "OUT_DIR must not be inside the snapshot" >&2; exit 2 ;; esac

# Start from a clean OUT_DIR (only the paths this script writes).
rm -rf "$OUT/export" "$OUT/diffs" "$OUT/logs" "$OUT/media" "$OUT/src" "$OUT/adapter" \
       "$OUT/db.sqlite3" "$OUT/platform.json" "$OUT/events.jsonl" "$OUT/stream.json" \
       "$OUT/report.json" "$OUT/files.tsv" "$OUT/summary.json" "$OUT/step.txt" "$OUT"/*.rc
mkdir -p "$OUT/logs" "$OUT/adapter"

TAG="e2e-zer0rt-$$"
NET=""; REDIS_C=""; PG_C=""; SUMMARY_PRINTED=0; STEP="setup"
cleanup() {
  rc=$?
  for c in "$TAG-app" "$REDIS_C" "$PG_C"; do
    [ -n "$c" ] && docker rm -f -v "$c" >/dev/null 2>&1 || true
  done
  [ -n "$NET" ] && docker network rm "$NET" >/dev/null 2>&1 || true
  if [ "$SUMMARY_PRINTED" != 1 ]; then
    secs=$(awk -v a="$T0" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}')
    printf '{"ok":false,"files_total":0,"files_identical":0,"files_different":0,"files_unsupported":0,"seconds":%s,"diff_samples":["step %s failed (exit %s): see %s/logs"],"events":null}\n' \
      "$secs" "$STEP" "$rc" "$OUT"
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not found locally" >&2; exit 3; }

# ── platform code: HEAD of zer0-cms, read-only ─────────────────────────────
STEP="code-snapshot"
CODE_MOUNTS=()
CODE_DESC="image:$IMAGE (its own code)"
if [ "${ZER0CMS_IMAGE_CODE:-0}" != 1 ]; then
  if [ -n "${ZER0CMS_SRC:-}" ]; then
    SRC=$(cd "$ZER0CMS_SRC" && pwd -P); CODE_DESC="dir:$SRC"
  else
    SRC="$OUT/src"; mkdir -p "$SRC"
    python3 "$(dirname "$0")/../snapshot.py" copy "$ZREPO" "$SRC" content schemas
    CODE_DESC="worktree:$(python3 "$(dirname "$0")/../snapshot.py" id "$ZREPO")@$(git -C "$ZREPO" rev-parse --short HEAD)"
  fi
  [ -f "$SRC/content/manage.py" ] && [ -d "$SRC/schemas/events" ] || { echo "$SRC lacks content/ or schemas/" >&2; exit 3; }
  CODE_MOUNTS=(-v "$SRC/content:/app/content:ro" -v "$SRC/schemas:/app/schemas:ro")
fi
log "code: $CODE_DESC"

# ── throwaway services ─────────────────────────────────────────────────────
STEP="services"
DB_URL="sqlite:////work/db.sqlite3"
RELAY=memory
REDIS_URL="redis://localhost:6379/0"
if [ "$EVENTS" = 1 ] || [ "$DB" = postgres ]; then
  NET="$TAG-net"
  docker network create "$NET" >/dev/null
fi
if [ "$EVENTS" = 1 ]; then
  REDIS_C="$TAG-redis"
  docker run -d --name "$REDIS_C" --network "$NET" redis:7-alpine >/dev/null
  for _ in $(seq 1 50); do docker exec "$REDIS_C" redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 0.2; done
  RELAY=redis; REDIS_URL="redis://$REDIS_C:6379/0"
  log "redis: $REDIS_C"
fi
if [ "$DB" = postgres ]; then
  PG_C="$TAG-pg"
  docker run -d --name "$PG_C" --network "$NET" -e POSTGRES_USER=zer0 -e POSTGRES_PASSWORD=zer0 \
    -e POSTGRES_DB=zer0 --tmpfs /var/lib/postgresql/data pgvector/pgvector:pg16 >/dev/null
  for _ in $(seq 1 100); do docker exec "$PG_C" pg_isready -U zer0 -d zer0 >/dev/null 2>&1 && break; sleep 0.3; done
  sleep 1
  DB_URL="postgres://zer0:zer0@$PG_C:5432/zer0"
  log "postgres: $PG_C"
fi

# ── the helpers that run inside the container ──────────────────────────────
cat > "$OUT/adapter/driver.sh" <<'SH'
set -eu
cd /app/content
L=/work/logs
step() { echo "$1" > /work/step.txt; echo "[in-container] $1" >&2; }
ts() { python -c 'import time; print(f"{time.time():.3f}")'; }
echo "{" > /work/logs/timings.json.part
mark() { echo "\"$1\": $(ts)," >> /work/logs/timings.json.part; }
mark start
step migrate
python manage.py migrate --noinput > $L/migrate.log 2>&1
mark migrated
if [ "$OFFICIAL" = 1 ]; then
  step roundtrip_jekyll
  set +e
  python manage.py roundtrip_jekyll --path /site --site "${SITE_KEY}-rt" > $L/roundtrip_jekyll.log 2>&1
  echo $? > /work/roundtrip_jekyll.rc
  set -e
  mark official_roundtrip
fi
step import_jekyll
set +e
python manage.py import_jekyll --site "$SITE_KEY" --path /site > $L/import_jekyll.log 2>&1
echo $? > /work/import_jekyll.rc
set -e
mark imported
step export_jekyll
python manage.py export_jekyll --site "$SITE_KEY" --out /work/export --include-drafts > $L/export_jekyll.log 2>&1
mark exported
step inspect
python manage.py shell -c "exec(open('/adapter/inspect_platform.py').read())" > $L/inspect.log 2>&1
if [ "$EVENTS" = 1 ] && [ "$RELAY" = redis ]; then
  step relay_outbox
  : > $L/relay_outbox.log
  i=0
  while [ $i -lt 100 ]; do
    out=$(python manage.py relay_outbox --once --batch 500)
    [ -z "$out" ] && break
    echo "$out" >> $L/relay_outbox.log
    i=$((i + 1))
  done
  mark relayed
  step inspect_stream
  python manage.py shell -c "exec(open('/adapter/inspect_stream.py').read())" >> $L/inspect.log 2>&1
fi
step compare
python -I /adapter/compare.py > $L/compare.log 2>&1
mark compared
echo "\"end\": $(ts)}" >> /work/logs/timings.json.part
mv /work/logs/timings.json.part /work/logs/timings.json
step done
SH

cat > "$OUT/adapter/inspect_platform.py" <<'PY'
# Runs in `manage.py shell`: what the platform holds after import_jekyll.
import collections
import hashlib
import json
import os

from cms.models import SiteRootPage
from exporters.jekyll import path_for
from importers.models import ImportedSource
from outbox.models import OutboxEvent
from wagtail.models import Page

key = os.environ["SITE_KEY"]
root = SiteRootPage.objects.get(site_key=key)
files = {}
for s in ImportedSource.objects.filter(site_key=key).select_related("page"):
    page = s.page.specific
    cur = page.get_latest_revision_as_object()
    files[s.legacy_path] = {
        "page_id": page.pk,
        "page_type": page._meta.label,
        "live": page.live,
        "export_mode": "verbatim" if cur.compute_content_hash() == s.import_hash else "rendered",
        "stored_sha256": hashlib.sha256(s.source_text.encode("utf-8")).hexdigest(),
        "export_path": path_for(cur, root),
    }
tree = Page.objects.descendant_of(root, inclusive=True).specific()
page_ids = {p.pk: p._meta.label for p in tree}
events = list(OutboxEvent.objects.order_by("created_at", "id"))
with open("/work/events.jsonl", "w") as fh:
    for e in events:
        fh.write(json.dumps(e.envelope(), sort_keys=True) + "\n")
imported_ids = {f["page_id"] for f in files.values()}
by_kind = collections.Counter()
for e in events:
    pid = e.data.get("page_id") if isinstance(e.data, dict) else None
    if e.type != "content.page.published":
        by_kind["other"] += 1
    elif pid in imported_ids:
        by_kind["imported_page"] += 1
    elif pid in page_ids:
        by_kind["site_structure:" + page_ids[pid]] += 1
    else:
        by_kind["outside_site"] += 1
out = {
    "site_key": key,
    "files": files,
    "page_counts": dict(collections.Counter(page_ids.values())),
    "live_imported_pages": sum(1 for f in files.values() if f["live"]),
    "events": {
        "total": len(events),
        "types": dict(collections.Counter(e.type for e in events)),
        "by_kind": dict(by_kind),
        "imported_live_pages_with_event": len({e.data.get("page_id") for e in events if e.type == "content.page.published"} & {f["page_id"] for f in files.values() if f["live"]}),
    },
}
with open("/work/platform.json", "w") as fh:
    json.dump(out, fh, indent=1, sort_keys=True)
print(f"platform: {len(files)} imported files, {len(events)} outbox events")
PY

cat > "$OUT/adapter/inspect_stream.py" <<'PY'
# Runs in `manage.py shell` after relay_outbox: what reached the Redis stream,
# and whether every envelope passes the consumer-side schema check.
import collections
import json

import redis
from django.conf import settings
from outbox.models import OutboxEvent
from outbox.schemas import check_envelope

r = redis.Redis.from_url(settings.REDIS_URL)
stream = settings.EVENTS_STREAM
entries = r.xrange(stream)
types = collections.Counter()
invalid = []
for _id, fields in entries:
    env = json.loads(fields[b"event"])
    types[env["type"]] += 1
    try:
        check_envelope(env)
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        invalid.append(f"{env.get('id')}: {exc}")
out = {
    "stream": stream,
    "xlen": r.xlen(stream),
    "types": dict(types),
    "envelopes_invalid": invalid[:20],
    "envelopes_invalid_count": len(invalid),
    "outbox_undispatched_after_relay": OutboxEvent.objects.filter(dispatched_at__isnull=True).count(),
    "sample": json.loads(entries[0][1][b"event"]) if entries else None,
}
with open("/work/stream.json", "w") as fh:
    json.dump(out, fh, indent=1, sort_keys=True)
print(f"stream {stream}: {out['xlen']} entries, {len(invalid)} invalid")
PY

cat > "$OUT/adapter/compare.py" <<'PY'
"""Compare the snapshot's Jekyll content files with the platform's export.

Universe ("files_total"): every file Jekyll would render as a page or a
collection document — not excluded by _config.yml, not under a `_`/`.`
directory other than a declared collection, and starting with a YAML
front-matter line (`---`) — plus anything the importer took that this scan did
not count. Each is identical / different / unsupported (outside what
import_jekyll reads). Everything else (assets, _data, _includes, excluded
dirs, Markdown without front matter) is counted as `not_content` in report.json.
"""

import collections
import difflib
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

import yaml

SITE, EXPORT, WORK = Path("/site"), Path("/work/export"), Path("/work")
HAS_FM = re.compile(rb"\A---\s*\r?\n")
FENCE = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?\n?", re.S | re.M)
MARKDOWN = {".md", ".markdown"}

config = yaml.safe_load((SITE / "_config.yml").read_text(encoding="utf-8")) or {}
source = (SITE / str(config.get("source", "."))).resolve()
cdir = str(config.get("collections_dir", "") or "").strip("/")
cols = config.get("collections") or {}
cols = list(cols.keys()) if isinstance(cols, dict) else [str(c) for c in cols]
col_dirs = {"/".join(p for p in (cdir, f"_{c}") if p) for c in ["posts", *cols]}
exclude = [str(p).rstrip("/") for p in (config.get("exclude") or [])]
include = [str(p).rstrip("/") for p in (config.get("include") or [])]
platform = json.loads((WORK / "platform.json").read_text())
imported = platform["files"]


def excluded(rel):
    for pat in exclude:
        if fnmatch.fnmatch(rel, pat) or rel == pat or rel.startswith(pat + "/"):
            return True
    return False


def classify(rel):
    """None for a content candidate, else why it is not content."""
    parts = rel.split("/")
    if excluded(rel):
        return "excluded by _config.yml"
    in_col = any(rel.startswith(d + "/") for d in col_dirs)
    if in_col:
        inner = rel[len(next(d for d in col_dirs if rel.startswith(d + "/"))) + 1 :].split("/")
        if any(p.startswith(".") for p in inner):
            return "dot path"
    else:
        special = [p for p in parts if p.startswith((".", "_", "#")) or p.endswith("~")]
        if special and not any(rel == i or rel.startswith(i + "/") for i in include):
            return "underscore/dot path (theme, data, config)"
    with open(SITE / rel, "rb") as fh:
        head = fh.read(64)
    if not HAS_FM.match(head):
        return "static file (no front matter)"
    return None


def split_fm(text):
    m = FENCE.match(text)
    return (m.group(1), text[m.end():]) if m else (None, text)


def reason_for(rel, a: bytes, b: bytes):
    info = imported.get(rel, {})
    mode = info.get("export_mode", "?")
    if a.startswith(b"\xef\xbb\xbf"):
        return f"UTF-8 BOM in source ({mode})"
    if b"\r\n" in a and b"\r\n" not in b:
        return f"CRLF line endings normalised to LF ({mode})"
    try:
        ta, tb = a.decode("utf-8"), b.decode("utf-8")
    except UnicodeDecodeError:
        return f"not UTF-8 ({mode})"
    fa, ba = split_fm(ta)
    fb, bb = split_fm(tb)
    bits = []
    if fa != fb:
        try:
            da = yaml.load(fa or "", Loader=yaml.BaseLoader) or {}
            db = yaml.load(fb or "", Loader=yaml.BaseLoader) or {}
        except yaml.YAMLError:
            da, db = None, None
        if da is not None and da == db:
            bits.append("front matter re-serialised, same data (quoting/comments/flow style)")
        elif isinstance(da, dict) and isinstance(db, dict):
            changed = sorted(k for k in set(da) | set(db) if da.get(k) != db.get(k))
            bits.append("front matter values changed: " + ",".join(changed[:6]))
        else:
            bits.append("front matter changed")
    if ba != bb:
        if ba.strip() == bb.strip():
            bits.append("body leading/trailing whitespace normalised")
        elif re.sub(r"\n{2,}", "\n\n", ba).strip() == re.sub(r"\n{2,}", "\n\n", bb).strip():
            bits.append("body blank-line runs normalised")
        else:
            bits.append("body content changed")
    return f"{'; '.join(bits) or 'bytes differ'} [{mode}]"


import_errors = {}
try:
    for line in (WORK / "logs" / "import_jekyll.log").read_text().splitlines():
        m = re.match(r"\s+(\S.*?): (\w+Error|\w+Exception|changed in Wagtail.*)", line)
        if m:
            import_errors[m.group(1)] = line.strip()
except FileNotFoundError:
    pass

rows, not_content = [], collections.Counter()
seen = set()
for dirpath, dirnames, filenames in os.walk(SITE):
    dirnames.sort()
    for name in sorted(filenames):
        p = Path(dirpath) / name
        if p.is_symlink():
            not_content["symlink"] += 1
            continue
        rel = p.relative_to(SITE).as_posix()
        why = classify(rel)
        if why is not None and rel not in imported:
            not_content[why] += 1
            continue
        seen.add(rel)
for rel in imported:
    seen.add(rel)

for rel in sorted(seen):
    src = SITE / rel
    a = src.read_bytes() if src.is_file() else b""
    if rel in imported:
        out = EXPORT / imported[rel]["export_path"]
        if not out.is_file():
            rows.append((rel, "different", f"missing from export (expected at {imported[rel]['export_path']})", a, b""))
            continue
        b = out.read_bytes()
        if a == b:
            rows.append((rel, "identical", imported[rel]["export_mode"], a, b))
        else:
            rows.append((rel, "different", reason_for(rel, a, b), a, b))
    elif rel in import_errors:
        rows.append((rel, "different", "import error: " + import_errors[rel], a, b""))
    else:
        ext = Path(rel).suffix.lower()
        if ext not in MARKDOWN:
            why = f"non-Markdown page ({ext or 'no extension'}); import_jekyll reads .md/.markdown only"
        elif not FENCE.match(a.decode("utf-8", "replace")):
            why = "front matter not closed by a second --- fence"
        else:
            why = "Markdown with front matter not imported (unknown)"
        rows.append((rel, "unsupported", why, a, b""))

exported = {p.relative_to(EXPORT).as_posix() for p in EXPORT.rglob("*") if p.is_file()} if EXPORT.is_dir() else set()
extra_exports = sorted(exported - {v["export_path"] for v in imported.values()})

diffdir = WORK / "diffs"
diffdir.mkdir(exist_ok=True)
with open(diffdir / "ALL.diff", "w", encoding="utf-8") as allfh:
    for rel, status, why, a, b in rows:
        if status != "different" or not b:
            continue
        diff = "".join(
            difflib.unified_diff(
                a.decode("utf-8", "replace").splitlines(keepends=True),
                b.decode("utf-8", "replace").splitlines(keepends=True),
                fromfile=f"snapshot/{rel}",
                tofile=f"export/{rel}",
            )
        )
        target = diffdir / (rel + ".diff")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(diff, encoding="utf-8")
        allfh.write(diff)

with open(WORK / "files.tsv", "w", encoding="utf-8") as fh:
    fh.write("path\tstatus\tdetail\n")
    for rel, status, why, _a, _b in rows:
        fh.write(f"{rel}\t{status}\t{why}\n")

count = collections.Counter(r[1] for r in rows)
diff_reasons = collections.Counter(re.sub(r": [^;\[]*", "", r[2]) for r in rows if r[1] == "different")
unsup_reasons = collections.Counter(r[2] for r in rows if r[1] == "unsupported")

# up to 5 samples, one per distinct reason first
samples, used = [], set()
different = [r for r in rows if r[1] == "different"]
for rel, _s, why, _a, _b in different:
    if why not in used and len(samples) < 5:
        samples.append(f"{rel}: {why}")
        used.add(why)
for rel, _s, why, _a, _b in different:
    if len(samples) >= 5:
        break
    s = f"{rel}: {why}"
    if s not in samples:
        samples.append(s)

events = None
if os.environ.get("EVENTS") == "1":
    ev = platform["events"]
    events = {"emitted": ev["total"], "types": sorted(ev["types"])}

import_rc = int((WORK / "import_jekyll.rc").read_text().strip() or 1)
ok = count["different"] == 0 and count["identical"] > 0 and import_rc == 0
summary = {
    "ok": ok,
    "files_total": len(rows),
    "files_identical": count["identical"],
    "files_different": count["different"],
    "files_unsupported": count["unsupported"],
    "seconds": None,
    "diff_samples": samples,
    "events": events,
}
official = None
if (WORK / "roundtrip_jekyll.rc").is_file():
    official = {
        "exit": int((WORK / "roundtrip_jekyll.rc").read_text().strip()),
        "log_tail": (WORK / "logs" / "roundtrip_jekyll.log").read_text().splitlines()[-3:],
    }
stream = json.loads((WORK / "stream.json").read_text()) if (WORK / "stream.json").is_file() else None
report = {
    "summary": summary,
    "import_jekyll_exit": import_rc,
    "import_summary": (WORK / "logs" / "import_jekyll.log").read_text().splitlines()[:1],
    "export_summary": (WORK / "logs" / "export_jekyll.log").read_text().splitlines()[:1],
    "official_roundtrip_jekyll": official,
    "export_modes": dict(collections.Counter(v["export_mode"] for v in imported.values())),
    "page_counts": platform["page_counts"],
    "different_by_reason": dict(diff_reasons.most_common()),
    "unsupported_by_reason": dict(unsup_reasons.most_common()),
    "unsupported_files": [r[0] for r in rows if r[1] == "unsupported"],
    "not_content_by_reason": dict(not_content.most_common()),
    "exported_files_not_in_snapshot": extra_exports,
    "events_detail": platform["events"],
    "relay": stream,
}
(WORK / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
(WORK / "summary.json").write_text(json.dumps(summary, separators=(",", ":")) + "\n", encoding="utf-8")
print(json.dumps(summary))
sys.exit(0)
PY

# ── the run ────────────────────────────────────────────────────────────────
STEP="platform"
log "site: $SNAP  out: $OUT  db: $DB  events: $EVENTS  official: $OFFICIAL"
NETARGS=(--network none)
[ -n "$NET" ] && NETARGS=(--network "$NET")
set +e
docker run --rm --name "$TAG-app" "${NETARGS[@]}" \
  ${CODE_MOUNTS[@]+"${CODE_MOUNTS[@]}"} \
  -v "$SNAP:/site:ro" -v "$OUT:/work" -v "$OUT/adapter:/adapter:ro" \
  -e DJANGO_SETTINGS_MODULE=config.settings.development \
  -e SECRET_KEY=e2e-throwaway -e DATABASE_URL="$DB_URL" -e MEDIA_ROOT=/work/media \
  -e REDIS_URL="$REDIS_URL" -e CACHE_URL=locmemcache:// -e OUTBOX_RELAY="$RELAY" \
  -e LLM_BACKEND=recorded -e EMBEDDING_BACKEND=hashing -e LOG_FORMAT=text -e LOG_LEVEL=WARNING \
  -e SITE_KEY="$SITE_KEY" -e EVENTS="$EVENTS" -e RELAY="$RELAY" -e OFFICIAL="$OFFICIAL" \
  --entrypoint sh "$IMAGE" /adapter/driver.sh >"$OUT/logs/container.log" 2>&1
rc=$?
set -e
if [ $rc -ne 0 ] || [ ! -f "$OUT/summary.json" ]; then
  STEP="container:$(cat "$OUT/step.txt" 2>/dev/null || echo '?')"
  tail -20 "$OUT/logs/container.log" >&2 || true
  exit 4
fi

STEP="summary"
SECS=$(awk -v a="$T0" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}')
printf '%s' "$CODE_DESC" > "$OUT/logs/code.txt"
LINE=$(sed "s/\"seconds\":null/\"seconds\":$SECS/" "$OUT/summary.json")
printf '%s\n' "$LINE" > "$OUT/summary.json"
log "files: $OUT/files.tsv  report: $OUT/report.json  diffs: $OUT/diffs/  logs: $OUT/logs/"
SUMMARY_PRINTED=1
echo "$LINE"
case "$LINE" in '{"ok":true'*) exit 0 ;; *) exit 1 ;; esac
