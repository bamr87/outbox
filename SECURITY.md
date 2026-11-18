# Security Policy

> How to report a vulnerability in outbox privately, which versions get security fixes, and how soon you will hear back.

## Supported versions

| Version | Supported |
| --- | --- |
| The tip of `main` and the latest release | ✅ |
| Anything older | ❌ |

Security fixes land on `main` and ship in the next release, which release-please cuts.

## Reporting a vulnerability

**Do not open a public issue.** Report privately through GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability) (**Security → Report a vulnerability**) when it is enabled on this repository; otherwise email **amr.abdel@gmail.com**.

Please include the version or commit, what an attacker can do, and the steps to reproduce: the `outbox.cli` command you ran, the task card, and an excerpt of the trial's `runs/<run-id>/ledger.jsonl`. Leave out secrets, keys and tokens.

The harness is offline and key-free by default: the policy bots stand in for live agents, and the tests make no network calls. A live Grok agent key never belongs in a task card, a ledger or `runs/`, so a path that writes one there is in scope. The `e2e/` adapters run sibling repositories' own tooling on throwaway snapshots; a flaw in that tooling belongs to its repository.

## Response window

You will get an acknowledgement **within 7 days**. We then agree a fix timeline with you, and credit you in the advisory unless you ask us not to.
