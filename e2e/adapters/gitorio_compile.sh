#!/usr/bin/env bash
# gitorio_compile.sh BLUEPRINT_JSON OUT_DIR
#
# Compiles a GitFactory blueprint with gitorio's REAL compiler (app/src/compiler/compile.ts)
# inside node:22-slim, writes each generated workflow to OUT_DIR, and prints ONE JSON summary
# as the last stdout line:
#   {"ok":bool,"workflows":[...],"errors":[...],"warnings":[...],
#    "guards":{"bot_guard":bool,"permissions":{...},"kill_switch_vars":[...],"never_merges":bool},
#    "seconds":S}
# Exit: 0 ok, 1 compile errors (nothing written), 2 adapter/usage/infra failure.
#
# The compiler runs from a read-only copy of the gitorio working tree in runs/.cache/gitorio-src
# (e2e/snapshot.py; refreshed whenever the tree changes). The gitorio checkout itself is never written.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${GITORIO_SRC_CACHE:-$HERE/../../runs/.cache/gitorio-src}"
GITORIO_REPO="${GITORIO_REPO:-$HERE/../../../gitorio}"
IMAGE="${GITORIO_NODE_IMAGE:-node:22-slim}"
NPM_VOL="${GITORIO_NPM_VOLUME:-e2e-gitorio-npm}"

now() { python3 -I -c 'import time; print(f"{time.time():.3f}")'; }
T0="$(now)"

fail_json() { # $1 = message; prints the summary shape with ok:false
  python3 -I -c '
import json, sys, time
print(json.dumps({"ok": False, "workflows": [], "errors": [sys.argv[1]], "warnings": [],
  "guards": {"bot_guard": False, "permissions": {}, "kill_switch_vars": [], "never_merges": True},
  "seconds": round(time.time() - float(sys.argv[2]), 3)}, ensure_ascii=False))' "$1" "$T0"
}

if [ "$#" -ne 2 ]; then
  echo "usage: $(basename "$0") BLUEPRINT_JSON OUT_DIR" >&2
  fail_json "adapter: usage: gitorio_compile.sh BLUEPRINT_JSON OUT_DIR"
  exit 2
fi
BP="$1"
OUT="$2"
if [ ! -f "$BP" ]; then
  fail_json "adapter: blueprint not found: $BP"
  exit 2
fi
mkdir -p "$OUT"
BP_ABS="$(cd "$(dirname "$BP")" && pwd)/$(basename "$BP")"
OUT_ABS="$(cd "$OUT" && pwd)"

# Header label (the `# blueprint:` line): GITORIO_BLUEPRINT_LABEL, else the repo-relative
# .factory/ path when there is one, else the file's basename.
case "$BP_ABS" in
  */.factory/*) LABEL=".factory/${BP_ABS##*/.factory/}" ;;
  *) LABEL="$(basename "$BP_ABS")" ;;
esac
LABEL="${GITORIO_BLUEPRINT_LABEL:-$LABEL}"

# Re-snapshot whenever the gitorio working tree changes (e2e/snapshot.py); node_modules survives a
# refresh while app/package-lock.json is unchanged, so only a lockfile change pays for npm ci again.
SNAP_ID="$(python3 "$HERE/../snapshot.py" id "$GITORIO_REPO")"
if [ "$(cat "$SRC/.snapshot-id" 2>/dev/null)" != "$SNAP_ID" ]; then
  echo "[adapter] snapshotting gitorio working tree $SNAP_ID from $GITORIO_REPO" >&2
  # Build the new snapshot beside the old one and swap it in whole. The checkout tracks
  # app/node_modules as a dangling symlink; it is never copied, because Docker Desktop's file
  # sharing can keep seeing a symlink at that path after it is replaced, which puts the tmpfs
  # mounts below somewhere else and leaves Vite writing into the read-only snapshot.
  NEW="$SRC.new"
  rm -rf "$NEW"
  python3 "$HERE/../snapshot.py" copy "$GITORIO_REPO" "$NEW" . ':(exclude)app/node_modules'
  rm -f "$NEW/app/node_modules"
  if [ -d "$SRC/app/node_modules" ] && [ ! -L "$SRC/app/node_modules" ] \
     && cmp -s "$SRC/app/package-lock.json" "$NEW/app/package-lock.json"; then
    mv "$SRC/app/node_modules" "$NEW/app/node_modules"
  fi
  echo "$SNAP_ID" > "$NEW/.snapshot-id"
  rm -rf "$SRC"
  mv "$NEW" "$SRC"
fi
if [ ! -x "$SRC/app/node_modules/.bin/vite-node" ]; then
  echo "[adapter] npm ci for the gitorio snapshot" >&2
  docker volume create "$NPM_VOL" >/dev/null
  docker run --rm -v "$SRC:/gitorio" -v "$NPM_VOL:/root/.npm" -w /gitorio/app "$IMAGE" \
    npm ci --no-audit --no-fund >&2
fi
# Vite writes its bundled config to node_modules/.vite-temp; the snapshot is mounted read-only,
# so those cache dirs are tmpfs mounts (their mountpoints must exist in the snapshot).
mkdir -p "$SRC/app/node_modules/.vite-temp" "$SRC/app/node_modules/.vite"
echo "[adapter] gitorio snapshot $SNAP_ID · image $IMAGE · label $LABEL" >&2

compile_once() {
  docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$SRC:/gitorio:ro" \
    --tmpfs /gitorio/app/node_modules/.vite-temp:mode=1777 --tmpfs /gitorio/app/node_modules/.vite:mode=1777 \
    -v "$HERE/driver:/driver:ro" \
    -v "$BP_ABS:/in/blueprint.json:ro" \
    -v "$OUT_ABS:/out" \
    -e BP_FILE=/in/blueprint.json -e OUT_DIR=/out -e BP_LABEL="$LABEL" \
    -w /gitorio/app "$IMAGE" \
    node_modules/.bin/vite-node /driver/compile-driver.ts 2>"$OUT_ABS/.compile-stderr"
}

rc=0
STDOUT="$(compile_once)" || rc=$?
# Right after a snapshot refresh, Docker Desktop's file sharing can still show the container the
# old node_modules for a moment, so Vite's tmpfs-backed temp dir is missed and the write hits the
# read-only snapshot (EROFS). That is the host's file cache, not the compile: retry once, logged.
if [ "$rc" -ne 0 ] && grep -q EROFS "$OUT_ABS/.compile-stderr"; then
  echo "[adapter] EROFS from a stale file-sharing view after the snapshot refresh; retrying once" >&2
  sleep 2
  rc=0
  STDOUT="$(compile_once)" || rc=$?
fi
cat "$OUT_ABS/.compile-stderr" >&2
rm -f "$OUT_ABS/.compile-stderr"

LAST="$(printf '%s\n' "$STDOUT" | tail -n 1)"
if ! printf '%s' "$LAST" | python3 -I -c 'import json,sys; d=json.load(sys.stdin); assert "ok" in d' 2>/dev/null; then
  printf '%s\n' "$STDOUT" >&2
  fail_json "adapter: driver produced no JSON summary (docker/vite-node exit $rc)"
  exit 2
fi
# Pass through any other stdout, then the summary with wall-clock seconds (incl. container start).
printf '%s\n' "$STDOUT" | sed '$d'
printf '%s' "$LAST" | python3 -I -c '
import json, sys, time
d = json.load(sys.stdin)
d["seconds"] = round(time.time() - float(sys.argv[1]), 3)
print(json.dumps(d, ensure_ascii=False))' "$T0"
exit "$rc"
