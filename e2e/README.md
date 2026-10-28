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

## The live platform run

`platform_run.py` runs the same journey with zer0-cms as the live origin instead of a file-level stand-in: the platform runs as its own Docker Compose stack (Postgres, Redis, the web role, the Celery worker and beat, the outbox relay and the belt publisher), and a person signed in to its API starts every piece of work.

```bash
python3 e2e/platform_run.py                      # build and start zer0-cms, run every stage, tear the stack down
python3 e2e/platform_run.py --keep-up            # leave the stack on http://127.0.0.1:18000 (the studio's Outbox area included)
python3 e2e/platform_run.py --out DIR --resume   # reuse a stack a --keep-up run left up (same --out): no build, no re-import
python3 e2e/platform_run.py --only toolbelt harness orchestrate   # just those stages
```

| Stage | Where it runs | What it shows |
| --- | --- | --- |
| `platform-up`, `platform-import` | zer0-cms (compose) | The stack is healthy; a person signs in; the platform imports lifehacker.dev |
| `sense` | zer0-cms Outbox desk | The person sends a hack idea and a Wire idea onto the belt |
| `plan` | lifehacker.dev (`ruby:3.4`) | Its scout and Wire backlog builders mint the ids as the belt's single planner; `lint_artifacts.rb` passes |
| `produce` | zer0-cms studio API | The person writes both pieces as draft pages: typed fields, raw front matter, file path |
| `verify` | zer0-cms worker (outbox claim desk), lifehacker.dev gates | A claim is blocked, corrected in the platform and re-checked; the exported drafts pass lifehacker.dev's gates; the person approves |
| `publish` | zer0-cms, then lifehacker.dev (`node:22`, Jekyll) | Publishing announces each page on the belt through the transactional outbox; the export lands on a lifehacker.dev branch with cover art; gates, build and html-proofer pass |
| `learn` | zer0-cms worker | Claims resolve into calibration; a recheck blocks a published claim and the person signals a correction |
| `orchestrate`, `toolbelt`, `harness` | gitorio (`node:22`), scripts (`ruby:3.4`, shellcheck), outbox (`python:3.13`) | The lines compile with their guards; the toolbelt's safety smoke; the harness's own suite |
| `ledger` | the belt | Each piece's journey runs idea to published (and, for the dispatch, to the correction) on one gapless ledger |

The host only drives HTTP and Docker, and plays the planner, drafter and verifier stations with the outbox library on the belt directory the platform shares; the platform's relay stays the belt's only writer. The run's posts, cover art and backlog entries are left uncommitted on a new lifehacker.dev branch `e2e/platform-run-<timestamp>`. They are test output: the Wire story is fictional, so that branch is never to be merged.

## Layout

- `run.py` — the runner; writes `report.json`, `report.md`, the belt ledger and every adapter log under its output directory.
- `platform_run.py` — the live platform run (above); writes `report.json`, `report.md`, the compose logs and the belt under its output directory.
- `adapters/` — one script per sibling repo. Each takes paths as arguments, works on a copy made by `snapshot.py`, and prints one JSON summary as its last line.
- `snapshot.py` — copies a sibling repo's working tree (or `HEAD` with `E2E_SNAPSHOT=head`) and fingerprints it, so the gitorio compiler cache refreshes when the tree changes.
- `fixtures/` — synthetic scout ideas, a fictional Wire story with a source timeline, and the post templates the drafter fills.
- `blueprints/` — the scout, drafter and content lines gitorio compiles.

Fixtures are synthetic: sources point at `example.org`, and the Wire story's company and model do not exist. Nothing the runner writes is published.
