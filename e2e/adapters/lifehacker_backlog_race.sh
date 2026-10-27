#!/usr/bin/env bash
# lifehacker_backlog_race.sh LIFEHACKER_REPO IDEAS_A IDEAS_B IDEAS_BELT OUT_DIR
# Reproduces lifehacker.dev's backlog id race with its own code (build_backlog.rb,
# merge_backlog.rb, lint_artifacts.rb) in ruby:3.4, against a snapshot of HEAD:
#   branches  - two scout runs branched off the same main, merged with the real driver
#   union     - the same two branches under the old merge=union strategy
#   belt      - one planner run over every idea in relay order (the belt's single writer)
# Prints one JSON summary as its last stdout line.
set -euo pipefail
REPO=$(cd "$1" && pwd); A=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
B=$(cd "$(dirname "$3")" && pwd)/$(basename "$3"); BELT=$(cd "$(dirname "$4")" && pwd)/$(basename "$4")
OUT=$(mkdir -p "$5" && cd "$5" && pwd)
rm -rf "$OUT/snap"; mkdir -p "$OUT/snap" "$OUT/in"
python3 "$(dirname "$0")/../snapshot.py" copy "$REPO" "$OUT/snap"
cp "$A" "$OUT/in/a.jsonl"; cp "$B" "$OUT/in/b.jsonl"; cp "$BELT" "$OUT/in/belt.jsonl"
start=$(date +%s)
docker run --rm -v "$OUT/snap:/src:ro" -v "$OUT/in:/in:ro" -v "$OUT:/out" ruby:3.4 bash -c '
set -uo pipefail
ids() { grep -oE "^  - id: SRC-[0-9]+" _data/backlog.yml | awk "{print \$3}" | sort | tr "\n" " "; }
prep() { rm -rf "/work/$1"; cp -r /src "/work/$1"; cd "/work/$1"; git init -q -b main; git config user.name e2e; git config user.email e2e@example.invalid; git add -A; git commit -qm base; }
mint() { git checkout -qb "$1" main; cp "/in/$2" _data/scout/ideas.jsonl; ruby scripts/scout/build_backlog.rb --apply --max-add "${3:-10}" > "/out/mint-$1.txt" 2>&1; git commit -qam "$1"; }
mkdir -p /work
base_ids=$(cd /src && grep -oE "^  - id: SRC-[0-9]+" _data/backlog.yml | awk "{print \$3}" | sort | tr "\n" " ")

# 1. real driver
prep driver
git config merge.backlog.name "backlog.yml item-block merge"
git config merge.backlog.driver "ruby /work/driver/scripts/ci/merge_backlog.rb %O %A %B"
mint scout-a a.jsonl; a_ids=$(ids); mint scout-b b.jsonl; b_ids=$(ids)
git checkout -q scout-a; git merge -q --no-edit scout-b > /out/merge-driver.txt 2>&1; driver_rc=$?
driver_state=$(git status --porcelain _data/backlog.yml)

# 2. old union strategy
prep union
printf "_data/backlog.yml merge=union\n" > .gitattributes; git commit -qam union-era
mint scout-a a.jsonl; mint scout-b b.jsonl
git checkout -q scout-a; git merge -q --no-edit scout-b > /out/merge-union.txt 2>&1; union_rc=$?
ruby scripts/ci/lint_artifacts.rb > /out/lint-union.txt 2>&1; union_lint_rc=$?
union_ids=$(grep -oE "^  - id: SRC-[0-9]+" _data/backlog.yml | awk "{print \$3}" | sort | uniq -d | tr "\n" " ")

# 3. belt: one serialized planner run over every idea
prep belt
mint planner belt.jsonl 10; belt_ids=$(ids)
ruby scripts/ci/lint_artifacts.rb > /out/lint-belt.txt 2>&1; belt_lint_rc=$?
belt_dups=$(grep -oE "^  - id: SRC-[0-9]+" _data/backlog.yml | awk "{print \$3}" | sort | uniq -d | tr "\n" " ")

cat > /out/race.env <<ENV
base_ids="$base_ids"
a_ids="$a_ids"
b_ids="$b_ids"
driver_rc=$driver_rc
driver_state="$driver_state"
union_rc=$union_rc
union_lint_rc=$union_lint_rc
union_dups="$union_ids"
belt_ids="$belt_ids"
belt_lint_rc=$belt_lint_rc
belt_dups="$belt_dups"
ENV
' > "$OUT/container.log" 2>&1
python3 - "$OUT" "$start" <<'PY'
import json, shlex, sys, time, pathlib
out = pathlib.Path(sys.argv[1]); start = int(sys.argv[2])
env = {}
for line in (out / "race.env").read_text().splitlines():
    k, _, v = line.partition("=")
    env[k] = shlex.split(v)[0] if v.strip() not in ('""', "") else ""
new = lambda ids: sorted(set(ids.split()) - set(env["base_ids"].split()))
a, b, belt = new(env["a_ids"]), new(env["b_ids"]), new(env["belt_ids"])
summary = {
    "branches": {"scout_a_minted": a, "scout_b_minted": b, "same_ids_minted": sorted(set(a) & set(b)),
                 "merge_exit": int(env["driver_rc"]), "backlog_status": env["driver_state"],
                 "outcome": "conflict (needs-human)" if int(env["driver_rc"]) else "merged"},
    "union": {"merge_exit": int(env["union_rc"]), "duplicate_ids": env["union_dups"].split(),
              "lint_artifacts_exit": int(env["union_lint_rc"])},
    "belt": {"minted": belt, "duplicate_ids": env["belt_dups"].split(), "lint_artifacts_exit": int(env["belt_lint_rc"])},
    "seconds": round(time.time() - start, 1),
}
summary["ok"] = (summary["belt"]["lint_artifacts_exit"] == 0 and not summary["belt"]["duplicate_ids"]
                 and len(belt) == len(a) + len(b))
(out / "summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary))
PY
