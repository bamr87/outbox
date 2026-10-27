#!/usr/bin/env bash
# =============================================================================
# lifehacker_gates.sh — run lifehacker.dev's own content gates on a snapshot
# -----------------------------------------------------------------------------
# Runs the repo's UNMODIFIED harness (scripts/ci/run-all.sh with LH_SKIP_BUILD=1:
# every lint it lists, then aggregate.rb) inside ruby:3.4 (+ node, which
# lint_preview.rb shells out to, as on GitHub's ubuntu-latest), under
# `bundle exec` of the repo's Gemfile.lock installed with the lockfile's own
# bundler version. The snapshot is mounted read-only and copied inside the
# container; nothing is written to SNAPSHOT_DIR. The harness output
# (findings.jsonl, summary.json, comment.md, every <check>.json) lands in
# OUT_DIR/test-results.
#
# Usage:
#   lifehacker_gates.sh SNAPSHOT_DIR OUT_DIR [CHANGED_PATH ...]
#
# Scoping (mirrors pipeline.yml `verify` on a pull_request):
#   The PR's changed-file list comes from CHANGED_PATH args (repo-relative), else
#   LH_CHANGED_FILES (inline list), else LH_BASE_DIR (a pristine snapshot that
#   SNAPSHOT_DIR is diffed against, like `git diff --name-only BASE...HEAD`).
#   Brand is then always scoped to that list; the rest of the report and the gate
#   are scoped to it only when classify_changes.rb calls the diff content-only.
#   No list => full-repo report and gate (the push-to-main behaviour).
#
# Optional build hand-off (full verify parity):
#   LH_SITE_DIR=<build OUT_DIR>/_site  LH_BUILD_RC=<lifehacker_build.sh exit code>
#   => htmlproofer and drift's _site checks run too; a failed build is the sev1.
#
# ok == CI verify's decision: the harness (aggregate.rb) exits 0, and, when a
# build was handed in, that build succeeded.
#
# Env: LH_IMAGE (default e2e-lh-ruby-node:3.4, built on demand),
#      LH_GEMS_VOLUME (default e2e-lh-gems; the gem cache).
# Last stdout line: one JSON summary. Logs: OUT_DIR/gates.log.
# =============================================================================
set -euo pipefail

START=$(date +%s)
if [[ $# -lt 2 ]]; then
  echo "usage: $0 SNAPSHOT_DIR OUT_DIR [CHANGED_PATH ...]" >&2
  exit 2
fi
SNAP="$(cd "$1" && pwd)"
mkdir -p "$2"
OUT="$(cd "$2" && pwd)"
shift 2

IMAGE="${LH_IMAGE:-e2e-lh-ruby-node:3.4}"
GEMS="${LH_GEMS_VOLUME:-e2e-lh-gems}"
LOG="$OUT/gates.log"
: > "$LOG"
log() { echo "[gates] $*" | tee -a "$LOG" >&2; }

fail_json() { # the summary line for a run where the harness never produced a verdict
  printf '{"ok":false,"error":"%s","checks_run":[],"checks_skipped":[],"findings":0,"by_severity":{},"seconds":%d}\n' \
    "$1" "$(( $(date +%s) - START ))"
  exit 1
}

[[ -f "$SNAP/scripts/ci/run-all.sh" ]] || fail_json "no scripts/ci/run-all.sh under $SNAP"
command -v docker >/dev/null || fail_json "docker not on PATH"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  log "building $IMAGE (ruby:3.4 + node from node:22-slim)"
  docker build -q -t "$IMAGE" - >>"$LOG" 2>&1 <<'EOF' || fail_json "could not build $IMAGE"
FROM node:22-slim AS node
FROM ruby:3.4
COPY --from=node /usr/local/bin/node /usr/local/bin/node
EOF
fi

# ---- inputs: changed-file list, base snapshot, built site --------------------
rm -rf "$OUT/test-results" "$OUT/changed.txt" "$OUT/gates-summary.json"
mkdir -p "$OUT/test-results"
if [[ $# -gt 0 ]]; then
  printf '%s\n' "$@" > "$OUT/changed.txt"
elif [[ -n "${LH_CHANGED_FILES:-}" ]]; then
  tr ' ,' '\n\n' <<<"$LH_CHANGED_FILES" | sed '/^$/d' > "$OUT/changed.txt"
fi
MOUNTS=(-v "$SNAP:/snap:ro" -v "$OUT:/out" -v "$GEMS:/usr/local/bundle")
if [[ ! -f "$OUT/changed.txt" && -n "${LH_BASE_DIR:-}" ]]; then
  MOUNTS+=(-v "$(cd "$LH_BASE_DIR" && pwd):/base:ro")
fi
HAVE_SITE=""
if [[ -n "${LH_SITE_DIR:-}" ]]; then
  [[ -d "$LH_SITE_DIR" ]] || fail_json "LH_SITE_DIR $LH_SITE_DIR is not a directory"
  MOUNTS+=(-v "$(cd "$LH_SITE_DIR" && pwd):/site:ro")
  HAVE_SITE=1
fi
log "snapshot=$SNAP out=$OUT image=$IMAGE gems=$GEMS site=${LH_SITE_DIR:-none}"

# ---- what runs inside the container ------------------------------------------
INNER=$(cat <<'INNER_EOF'
set -euo pipefail
exec 3>&1 1>>/out/gates.log 2>&1      # every log line to gates.log; fd 3 carries the JSON

# 1. A private, writable copy of the snapshot as a git work tree, so lint_oneline
#    (git ls-files --cached --others --exclude-standard) sees what a CI checkout
#    holds. With a base snapshot: commit the base, overlay the snapshot, and take
#    the staged diff — the list CI gets from `git diff --name-only BASE...HEAD`.
mkdir -p /work
if [[ -d /base && ! -f /out/changed.txt ]]; then
  cp -a /base/. /work/
  git -C /work init -q && git -C /work add -A
  git -C /work -c user.name=e2e -c user.email=e2e@local commit -qm base
  find /work -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  cp -a /snap/. /work/
  git -C /work add -A
  git -C /work -c core.quotepath=false diff --cached --name-only > /out/changed.txt
  [[ -s /out/changed.txt ]] || rm -f /out/changed.txt
else
  cp -a /snap/. /work/
  git -C /work init -q
fi
cd /work
if [[ -n "${HAVE_SITE:-}" ]]; then ln -s /site /work/_site; fi

# 2. The repo bundle (github-pages + html-proofer) with the lockfile's bundler,
#    cached in the gems volume. bundle exec puts html-proofer on every nested
#    `ruby`'s load path, as CI's setup-ruby bundler-cache does.
BV=$(awk '/^BUNDLED WITH/{getline; gsub(/ /,""); print}' Gemfile.lock)
gem list -i bundler -v "$BV" >/dev/null || gem install bundler -v "$BV" --no-document
export BUNDLE_PATH=/usr/local/bundle/lh-repo BUNDLE_FROZEN=true
bundle "_${BV}_" check >/dev/null 2>&1 || bundle "_${BV}_" install --jobs 4 --retry 2

# 3. Scope like pipeline.yml verify (pull_request) when there is a changed list.
export LH_RESULTS_DIR=/out/test-results LH_SKIP_BUILD=1 LH_BUILD_RC="$BUILD_RC"
KINDS=""
if [[ -f /out/changed.txt ]]; then
  KINDS=$(ruby scripts/ci/classify_changes.rb < /out/changed.txt)
  echo "changed kinds: [$KINDS]"; cat /out/changed.txt
  export LH_BRAND_CHANGED_FILES=/out/changed.txt
  case " $KINDS " in
    *" deps "*|*" pipeline "*) echo "infra/deps change: full-repo report (brand still scoped)" ;;
    *" content "*) export LH_CHANGED_FILES=/out/changed.txt; echo "content-only: scoped report + gate" ;;
    *) echo "no content kind: full-repo report (brand still scoped)" ;;
  esac
else
  export LH_BRAND_SCOPE_ALL=1
fi

# 4. The harness, unmodified. Its exit code is aggregate.rb's verdict.
hrc=0
bundle "_${BV}_" exec bash scripts/ci/run-all.sh || hrc=$?
echo "run-all.sh exit $hrc"

# 5. The summary line.
HRC=$hrc KINDS="$KINDS" ruby -rjson - > /out/gates-summary.json <<'RB'
r = '/out/test-results'
rd = ->(n) { (JSON.parse(File.read(File.join(r, n), encoding: 'UTF-8')) rescue nil) }
summary = rd.('summary.json') || {}
all = (File.readlines(File.join(r, 'findings.jsonl'), encoding: 'UTF-8').map { |l| JSON.parse(l) } rescue [])
site = !ENV['HAVE_SITE'].to_s.empty?
checks = %w[build frontmatter drift brand artifacts preview agents oneline wire tokens prime-directive htmlproofer]
skipped = []
skipped << { check: 'build', reason: 'this adapter does not build; run lifehacker_build.sh and pass LH_SITE_DIR + LH_BUILD_RC for full verify parity' } unless site
if (s = (rd.('htmlproofer.json') || []).find { |f| %w[no-site gem-missing].include?(f['rule']) })
  skipped << { check: 'htmlproofer', reason: s['evidence'] }
end
if (rd.('prime-directive.json') || []).any? { |f| f['rule'] == 'unverified-no-sandbox' }
  skipped << { check: 'prime-directive', reason: 'no Docker daemon inside the container, so lh:run shell blocks are reported info unverified-no-sandbox (the check is non-blocking in CI)' }
end
if (s = (rd.('preview.json') || []).find { |f| f['rule'] == 'motif-check-skipped' })
  skipped << { check: 'preview:motif', reason: s['evidence'] }
end
ran = checks.select { |c| File.exist?(File.join(r, "#{c}.json")) } - skipped.map { |s| s[:check] }
hrc = ENV['HRC'].to_i
ok = File.exist?(File.join(r, 'findings.jsonl')) && hrc.zero? && (!site || ENV['BUILD_RC'].to_i.zero?)

# The blocking findings, scoped the way aggregate.rb scopes the gate.
errs = all.select { |f| f['severity'] == 'error' }
if summary['scoped']
  chg = File.readlines('/out/changed.txt', encoding: 'UTF-8').map(&:strip).reject(&:empty?)
  slugs = chg.grep(%r{\Apages/_[a-z]+/.+\.md\z}).map { |p| File.basename(p, '.md').sub(/\A\d{4}-\d{2}-\d{2}-/, '') }
  errs = errs.select do |f|
    file = f['file'].to_s
    file.empty? || chg.include?(file) || slugs.any? { |s| file =~ %r{(\A|/)#{Regexp.escape(s)}(/|\.|\z)} }
  end
end

puts JSON.generate(
  ok: ok,
  gate: summary['gate'],
  harness_exit: hrc,
  scoped: summary['scoped'] || false,
  changed_kinds: ENV['KINDS'].to_s.split,
  checks_run: ran,
  checks_skipped: skipped,
  findings: summary['total'] || all.size,
  repo_total: summary['repo_total'] || all.size,
  by_severity: { 'error' => summary['error_count'].to_i, 'warning' => summary['warning_count'].to_i, 'info' => summary['info_count'].to_i },
  errors: errs.first(15).map { |f| f.slice('check_id', 'rule', 'file', 'line', 'evidence') },
  findings_jsonl: "#{r}/findings.jsonl",
  seconds: Time.now.to_i - ENV['START'].to_i
)
RB
cat /out/gates-summary.json >&3
INNER_EOF
)

rc=0
docker run --rm "${MOUNTS[@]}" \
  -e START="$START" -e BUILD_RC="${LH_BUILD_RC:-0}" -e HAVE_SITE="$HAVE_SITE" \
  -e TZ=UTC -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 \
  "$IMAGE" bash -c "$INNER" >/dev/null || rc=$?

if [[ -s "$OUT/gates-summary.json" ]]; then
  # In-container /out/... paths -> the host OUT_DIR, then the one JSON line.
  line=$(sed -e "s#\"/out/#\"$OUT/#g" "$OUT/gates-summary.json" | tail -n 1)
  printf '%s\n' "$line" > "$OUT/gates-summary.json"
  printf '%s\n' "$line"
  [[ "$line" == *'"ok":true'* ]] && exit 0 || exit 1
fi
fail_json "container run failed (exit $rc); see $LOG"
