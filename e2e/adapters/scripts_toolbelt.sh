#!/usr/bin/env bash
# scripts_toolbelt.sh SCRIPTS_REPO OUT_DIR
# Smoke-tests bamr87/scripts from a copy of its working tree inside a sandbox: HOME is
# a throwaway directory and `gh` is a recording stub, so nothing reaches GitHub or
# the operator's dotfiles. Prints one JSON summary as its last stdout line.
set -uo pipefail
REPO=$(cd "$1" && pwd); OUT=$(mkdir -p "$2" && cd "$2" && pwd)
start=$(date +%s)
SNAP="$OUT/snap"; SANDBOX="$OUT/home"; STUBS="$OUT/stubs"
rm -rf "$SNAP" "$SANDBOX" "$STUBS"; mkdir -p "$SNAP" "$SANDBOX" "$STUBS"
python3 "$(dirname "$0")/../snapshot.py" copy "$REPO" "$SNAP"
cat > "$STUBS/gh" <<STUB
#!/usr/bin/env bash
echo "gh \$*" >> "$OUT/gh-calls.log"
exit 1
STUB
chmod +x "$STUBS/gh"; : > "$OUT/gh-calls.log"
run() { HOME="$SANDBOX" PATH="$STUBS:$PATH" GIT_CONFIG_GLOBAL=/dev/null "$@" </dev/null; }

# 1. The repo's own gate.
(cd "$SNAP" && shellcheck --severity=warning *.sh FORKME/forkme.sh STASHME/stashme.sh) > "$OUT/shellcheck.txt" 2>&1
sc_rc=$?

# 2. Every script answers --help without side effects.
: > "$OUT/help.tsv"
for s in project-init.sh git_init.sh FORKME/forkme.sh STASHME/stashme.sh rename-directory.sh create_package.sh install.sh; do
  before=$(find "$SANDBOX" -mindepth 1 | sort)
  (cd "$SNAP" && run bash "$s" --help) > "$OUT/help-$(basename "$s").txt" 2>&1; rc=$?
  after=$(find "$SANDBOX" -mindepth 1 | sort)
  wrote=$(comm -13 <(echo "$before") <(echo "$after") | sed "s#$SANDBOX#~#" | tr '\n' ' ')
  printf '%s\t%s\t%s\n' "$s" "$rc" "$wrote" >> "$OUT/help.tsv"
done

# 3. forkme evidence path (dry run: no network).
(cd "$SNAP" && run bash FORKME/forkme.sh --strategy metadata --dry-run --no-fork https://github.com/bamr87/outbox) > "$OUT/forkme.txt" 2>&1
fk_rc=$?

# 4. Channel scaffold: the plan's zer0-site type, then a custom scaffold with GitHub creation switched off.
(cd "$SNAP" && run env NON_INTERACTIVE=true PROJECT_TYPE=zer0-site PROJECT_NAME=e2e-channel PROJECT_DIR="$OUT/zer0-site" \
  bash project-init.sh --non-interactive) > "$OUT/wizard-zer0-site.txt" 2>&1
z_rc=$?
(cd "$SNAP" && run env NON_INTERACTIVE=true PROJECT_TYPE=custom PROJECT_NAME=e2e-channel PROJECT_DIR="$OUT/channel" \
  GITHUB_USERNAME=e2e-sandbox GITHUB_EMAIL=e2e@example.invalid CREATE_GITHUB_REPO=false PUSH_TO_GITHUB=false \
  SETUP_DOCKER=false SETUP_CI=true LICENSE=MIT bash project-init.sh --non-interactive) > "$OUT/wizard-custom.txt" 2>&1
c_rc=$?

python3 - "$OUT" "$sc_rc" "$fk_rc" "$z_rc" "$c_rc" "$start" <<'PY'
import json, sys, time, pathlib
out, sc, fk, z, c, start = sys.argv[1], *map(int, sys.argv[2:6]), int(sys.argv[6])
o = pathlib.Path(out)
helps = []
for line in (o / "help.tsv").read_text().splitlines():
    script, rc, wrote = (line.split("\t") + [""])[:3]
    helps.append({"script": script, "exit": int(rc), "wrote": wrote.split()})
channel = o / "channel"
gh_calls = [l for l in (o / "gh-calls.log").read_text().splitlines() if l.strip()]
findings = []
for h in helps:
    if h["exit"] != 0:
        findings.append({"rule": "help-fails", "script": h["script"]})
    if h["wrote"]:
        findings.append({"rule": "help-has-side-effects", "script": h["script"], "wrote": h["wrote"]})
if any("repo create" in g for g in gh_calls):
    findings.append({"rule": "ignores-CREATE_GITHUB_REPO=false", "script": "project-init.sh", "calls": gh_calls})
if z == 0:
    findings.append({"rule": "zer0-site-type-unexpectedly-accepted", "script": "project-init.sh"})
summary = {
    "ok": sc == 0 and all(h["exit"] == 0 for h in helps) and fk == 0 and c == 0,
    "shellcheck_clean": sc == 0,
    "help": helps,
    "forkme_metadata_dry_run": fk == 0,
    "zer0_site_type_supported": z == 0,
    "custom_scaffold": {"exit": c, "files": sorted(str(p.relative_to(channel)) for p in channel.rglob("*") if p.is_file() and ".git/" not in str(p))[:40] if channel.exists() else [], "git_repo": (channel / ".git").exists()},
    "gh_calls_attempted": gh_calls,
    "findings": findings,
    "seconds": round(time.time() - start, 1),
}
(o / "summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary))
PY
