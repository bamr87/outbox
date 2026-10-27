#!/usr/bin/env bash
# =============================================================================
# lifehacker_build.sh — the Pages-parity overlay Jekyll build of a snapshot
# -----------------------------------------------------------------------------
# Runs the repo's UNMODIFIED scripts/ci/build.sh (what CI's build-overlay action
# and scripts/preview.sh use): overlay the snapshot onto a copy of the
# zer0-mistakes theme, strip _plugins, `bundle install` the theme's Gemfile, and
# `jekyll build --config _config.yml,_config_dev.yml --strict_front_matter`.
#
# Theme: a `git archive HEAD` copy of the local checkout LH_THEME_DIR (default
# the sibling ../zer0-mistakes) streamed into the container —
# the checkout is only read. With no local checkout, or LH_THEME_REMOTE=1,
# build.sh clones the theme's default branch itself, exactly as CI does.
# The snapshot is mounted read-only; nothing is written to SNAPSHOT_DIR.
#
# Usage:
#   lifehacker_build.sh SNAPSHOT_DIR OUT_DIR [POST_PATH ...]
#
# POST_PATH (repo-relative, e.g. pages/_posts/wire/2026-10-26-x.md) are posts that
# must render: each one's URL is resolved from its front matter (permalink, with
# :slug/:title/:year/:month/:day, else the posts collection default) and the
# built page must exist and carry the post's title. Any missing => ok:false.
#
# Env: LH_IMAGE (default ruby:3.4), LH_GEMS_VOLUME (default e2e-lh-gems),
#      LH_THEME_DIR, LH_THEME_REMOTE=1.
# Output: OUT_DIR/_site, OUT_DIR/build.log. Last stdout line: one JSON summary.
# =============================================================================
set -euo pipefail

START=$(date +%s)
if [[ $# -lt 2 ]]; then
  echo "usage: $0 SNAPSHOT_DIR OUT_DIR [POST_PATH ...]" >&2
  exit 2
fi
SNAP="$(cd "$1" && pwd)"
mkdir -p "$2"
OUT="$(cd "$2" && pwd)"
shift 2

IMAGE="${LH_IMAGE:-ruby:3.4}"
GEMS="${LH_GEMS_VOLUME:-e2e-lh-gems}"
THEME_DIR="${LH_THEME_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)/zer0-mistakes}"
LOG="$OUT/build.log"
: > "$LOG"
log() { echo "[build] $*" | tee -a "$LOG" >&2; }

fail_json() {
  printf '{"ok":false,"error":"%s","pages":0,"seconds":%d,"site_dir":"%s"}\n' \
    "$1" "$(( $(date +%s) - START ))" "$OUT/_site"
  exit 1
}

[[ -f "$SNAP/scripts/ci/build.sh" ]] || fail_json "no scripts/ci/build.sh under $SNAP"
command -v docker >/dev/null || fail_json "docker not on PATH"

rm -rf "$OUT/_site" "$OUT/build-summary.json"
printf '%s\n' "$@" > "$OUT/posts.txt"

# ---- the theme ----------------------------------------------------------------
THEME_SOURCE="clone"
THEME_SHA=""
if [[ "${LH_THEME_REMOTE:-0}" != "1" && -d "$THEME_DIR/_layouts" ]]; then
  if git -C "$THEME_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    THEME_SOURCE="local:$THEME_DIR (git archive HEAD)"
    THEME_SHA="$(git -C "$THEME_DIR" log -1 --format=%H)"
    theme_stream() { git -C "$THEME_DIR" archive --format=tar HEAD; }
  else
    THEME_SOURCE="local:$THEME_DIR (working tree)"
    theme_stream() { tar -C "$THEME_DIR" --exclude=.git --exclude=node_modules -cf - .; }
  fi
else
  theme_stream() { :; }        # empty stream: build.sh clones the default branch
fi
log "snapshot=$SNAP out=$OUT image=$IMAGE gems=$GEMS theme=$THEME_SOURCE ${THEME_SHA:0:12}"

INNER=$(cat <<'INNER_EOF'
set -euo pipefail
exec 3>&1 1>>/out/build.log 2>&1

# The theme copy (stdin tar) becomes build.sh's THEME_CACHE; build.sh reuses a
# cached checkout when it has _layouts and THEME_SHA is unset (preview.sh's path).
mkdir -p /tmp/zer0-theme
tar -x -C /tmp/zer0-theme 2>/dev/null || true
[[ -d /tmp/zer0-theme/_layouts ]] || rm -rf /tmp/zer0-theme

unset THEME_SHA   # build.sh would re-fetch a non-git cache that does not match it
export THEME_CACHE=/tmp/zer0-theme LH_BUILD_DIR=/tmp/lh-build LH_SITE_OUT=/out/_site
brc=0
bash /snap/scripts/ci/build.sh build || brc=$?
echo "build.sh exit $brc"
theme_sha=$(git -C /tmp/zer0-theme rev-parse HEAD 2>/dev/null || true)

BRC=$brc THEME_SHA_CLONED="$theme_sha" ruby -rjson -ryaml -rdate - > /out/build-summary.json <<'RB'
site = '/out/_site'
brc = ENV['BRC'].to_i
pages = Dir.exist?(site) ? Dir.glob(File.join(site, '**', '*.html')).size : 0

# Resolve each expected post's URL the way Jekyll does for this site.
def fm_of(path)
  raw = File.read(path, encoding: 'UTF-8')
  return {} unless raw =~ /\A---\s*\r?\n(.*?)\r?\n---\s*\r?\n/m
  y = YAML.respond_to?(:unsafe_load) ? YAML.unsafe_load($1) : YAML.load($1)
  y.is_a?(Hash) ? y : {}
rescue StandardError
  {}
end

posts = File.readlines('/out/posts.txt', encoding: 'UTF-8').map(&:strip).reject(&:empty?).map do |rel|
  src = File.join('/snap', rel)
  unless File.file?(src)
    next { source: rel, url: nil, exists: false, error: 'source file not in snapshot' }
  end
  fm = fm_of(src)
  base = File.basename(rel, File.extname(rel))
  m = base.match(/\A(\d{4})-(\d{2})-(\d{2})-(.+)\z/)
  slug = (fm['slug'] || (m && m[4]) || base).to_s
  y, mo, d = m ? m[1..3] : [nil, nil, nil]
  if (dt = (fm['date'].is_a?(Date) || fm['date'].is_a?(Time)) ? fm['date'] : (Date.parse(fm['date'].to_s) rescue nil))
    y, mo, d = dt.strftime('%Y'), dt.strftime('%m'), dt.strftime('%d')
  end
  url = fm['permalink'] ? fm['permalink'].to_s.dup : "/posts/#{y}/#{mo}/#{d}/#{slug}/"
  url = url.gsub(':slug', slug).gsub(':title', slug).gsub(':year', y.to_s).gsub(':month', mo.to_s)
           .gsub(':i_month', mo.to_i.to_s).gsub(':day', d.to_s).gsub(':i_day', d.to_i.to_s)
  html = url.end_with?('/') ? File.join(site, url, 'index.html') : File.join(site, url)
  html = File.join(site, "#{url}.html") if !File.exist?(html) && !url.end_with?('/')
  exists = File.file?(html)
  body = exists ? File.read(html, encoding: 'UTF-8') : ''
  title = fm['title'].to_s
  esc = title.gsub('&', '&amp;').gsub('"', '&quot;').gsub("'", '&#39;').gsub('<', '&lt;').gsub('>', '&gt;')
  title_ok = exists && (body.include?(title) || body.include?(esc) || body.include?(title.gsub("'", '&#8217;')))
  { source: rel, url: url, html: html, exists: exists, title_found: title_ok }
end

ok = brc.zero? && File.file?(File.join(site, 'index.html')) &&
     posts.all? { |p| p[:exists] && p[:title_found] }
puts JSON.generate(
  ok: ok,
  build_exit: brc,
  pages: pages,
  seconds: Time.now.to_i - ENV['START'].to_i,
  site_dir: site,
  theme: { source: ENV['THEME_SOURCE'], sha: ENV['LOCAL_THEME_SHA'].to_s.empty? ? ENV['THEME_SHA_CLONED'] : ENV['LOCAL_THEME_SHA'] },
  posts: posts,
  log: '/out/build.log'
)
RB
cat /out/build-summary.json >&3
INNER_EOF
)

rc=0
theme_stream | docker run --rm -i \
  -v "$SNAP:/snap:ro" -v "$OUT:/out" -v "$GEMS:/usr/local/bundle" \
  -e START="$START" -e THEME_SOURCE="$THEME_SOURCE" -e LOCAL_THEME_SHA="$THEME_SHA" \
  -e TZ=UTC -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 \
  "$IMAGE" bash -c "$INNER" >/dev/null || rc=$?

if [[ -s "$OUT/build-summary.json" ]]; then
  line=$(sed -e "s#\"/out/#\"$OUT/#g" "$OUT/build-summary.json" | tail -n 1)
  printf '%s\n' "$line" > "$OUT/build-summary.json"
  printf '%s\n' "$line"
  [[ "$line" == *'"ok":true'* ]] && exit 0 || exit 1
fi
fail_json "container run failed (exit $rc); see $LOG"
