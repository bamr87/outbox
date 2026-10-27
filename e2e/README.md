# e2e — local end-to-end proof of the platform plan

> Runs one hack and one Wire dispatch through Sense, Plan, Produce, Verify, Publish and Learn on the outbox belt, using each sibling repo's real tooling on throwaway copies of its working tree.

## Run it

```bash
python3 e2e/run.py                       # every stage -> runs/e2e-<timestamp>/report.md
python3 e2e/run.py --skip publish        # skip a stage (it is reported as skipped, never as passed)
python3 e2e/run.py --projects ~/src      # where the sibling repos live (default: the parent of this repo)
E2E_SNAPSHOT=head python3 e2e/run.py    # test each repo's last commit instead of its working tree
```

It needs the sibling checkouts `scripts`, `lifehacker.dev`, `zer0-cms` and `gitorio` next to this repo, and Docker with the images the adapters name (`ruby:3.4`, `node:22-slim`, and the zer0-cms and Jekyll images). Network access is used once to install gitorio's npm packages and lifehacker.dev's gems into Docker volumes. No stage calls a model, GitHub, or any live source.

Each stage tests what is on disk: tracked files plus untracked, non-ignored ones, so an uncommitted fix in a sibling repo is what runs. The `preflight` entry in the report records each repo's branch, HEAD and uncommitted paths, so a report always says exactly what it proved.

## What each stage proves

| Stage | Repo tooling | Plan claim it tests |
| --- | --- | --- |
| `outbox-tests` | outbox unit tests | The belt and claim desk work offline, including 3 concurrent senders and relays |
| `toolbelt` | `scripts` in a sandboxed `HOME` with a recording `gh` stub | The toolbelt is safe to automate: every `--help` is side-effect free and `CREATE_GITHUB_REPO=false` never reaches `gh repo create` (the stage fails otherwise) |
| `sense`, `plan` | lifehacker.dev `build_backlog.rb`, `merge_backlog.rb`, `lint_artifacts.rb` | Branch-parallel scouts mint colliding ids; one belt writer does not |
| `produce`, `verify` | lifehacker.dev content gates; the outbox claim desk | Drafts pass the real gates; a Wire dispatch under another persona's byline fails them; a stale claim is blocked, corrected and re-verified; factual pieces wait for an approval |
| `publish` | lifehacker.dev overlay build; zer0-cms `roundtrip_jekyll` | The pieces render; zer0-cms can mirror the site |
| `learn` | outbox resolver and recheck | Calibration is measurable; a fact that changes after publication routes a correction |
| `orchestrate` | gitorio's compiler | Each line compiles to guarded Actions YAML; multi-agent lines and lifehacker.dev's blueprint do not, yet |
| `ledger` | outbox belt | One gapless ledger traces the whole journey |

## Layout

- `run.py` — the runner; writes `report.json`, `report.md`, the belt ledger and every adapter log under its output directory.
- `adapters/` — one script per sibling repo. Each takes paths as arguments, works on a copy made by `snapshot.py`, and prints one JSON summary as its last line.
- `snapshot.py` — copies a sibling repo's working tree (or `HEAD` with `E2E_SNAPSHOT=head`) and fingerprints it, so the gitorio compiler cache refreshes when the tree changes.
- `fixtures/` — synthetic scout ideas, a fictional Wire story with a source timeline, and the post templates the drafter fills.
- `blueprints/` — the scout, drafter and content lines gitorio compiles.

Fixtures are synthetic: sources point at `example.org`, and the Wire story's company and model do not exist. Nothing the runner writes is published.
