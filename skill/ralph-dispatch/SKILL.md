---
name: ralph-dispatch
description: Operate and audit the Ralph Dispatch bounded research queue, including manifest validation and seeding, tiered model runs, queue status, campaign budgets, stop requests, event inspection, and needs_human triage. Use when the user asks to run or stop Ralph Dispatch, seed scouts or dossiers, inspect the dispatch queue, audit critic-gated results, investigate a dispatcher failure, or review a Ralph Dispatch database.
---

# Operate Ralph Dispatch

Work from the repository root containing `dispatch.py`. Treat the SQLite
database and evidence manifest as user data; do not expose their contents
unless the user asks.

## Run the workflow

1. Run `python -m unittest discover -s tests -v` before an unattended batch.
2. Validate a normalized manifest without writes:
   `python seed.py --manifest jobs.json --dry-run`.
3. Seed idempotently:
   `python seed.py --db dispatch.db --manifest jobs.json`.
4. Inspect state and persistent resource ceilings:
   `python dispatch.py --db dispatch.db status`.
5. Confirm `<database>.stop` is absent. Remove it only with an explicit resume
   request: `python dispatch.py --db dispatch.db resume`.
6. Run through Anthropic or an OpenAI-compatible local endpoint. Use
   `python dispatch.py run --help` for provider and budget flags.
7. Require a clean post-run audit:
   `python dispatch.py --db dispatch.db audit`.
8. Inspect transitions with
   `python dispatch.py --db dispatch.db events --job-id ID`.

Request a graceful halt with `python dispatch.py --db dispatch.db stop`. It
takes effect before the next model call; an in-flight HTTP request remains
bounded by its request timeout.

## Preserve the safety boundary

- Route batch model calls through the dispatcher; do not bypass its budgets,
  schemas, evidence binding, or critic gate.
- Never put credentials, tokens, passwords, or secrets in payloads or evidence.
- Never downgrade `T3_critic` or reinterpret `awaiting_review` as committed.
- Treat `needs_human` as a required decision. Report the job, last error, and
  event trail; do not auto-resolve or silently reseed it.
- Raise persistent campaign ceilings only when the user explicitly approves
  the new values. The runtime intentionally provides no usage-counter reset.
- Do not claim live-web verification: workers and critics receive bounded
  evidence packs and have no tools.
- Publish or merge only `committed` worker results from a database that passes
  `audit`; model text itself has no publishing authority.
