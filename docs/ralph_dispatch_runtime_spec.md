# Ralph Dispatch v2 — Runtime Specification

**Purpose:** bounded, auditable research synthesis on one host
**Design constraint:** deterministic sequential calls and model-resident tier
batches; no autonomous retrieval or publishing

## 1. Authority boundary

The dispatcher is the only component authorized to call models for a batch.
Models receive text and return text. They receive no tools, browser, filesystem,
credentials, database handle, or publishing capability. A separate supervised
process must create evidence packs.

Only a worker row in `committed` state is eligible for downstream merge. A raw
model response, a `running` row, an `awaiting_review` row, or a committed critic
row is not a publishable worker result.

## 2. Deterministic tiers

| Tier | Function | Default model | Calls at once |
|---|---|---|---:|
| T1 Extract | scout/extract structured facts | `claude-haiku-4-5-20251001` | 1 |
| T2 Synthesize | assemble dossiers; handle escalations | `claude-sonnet-4-6` | 1 |
| T3 Critic | independent evidence and task gate | `claude-opus-4-8` | 1 |

Model IDs are configurable through environment variables but the T3 tier is
never an automatic worker target and is never downgraded by the runtime. T0 was
removed: the prototype declared a routing tier but had no route job or call
path. Job-kind allowlisting and idempotency now perform that deterministic
boundary function without spending a model call.

The scheduler drains T1, then T2, then T3. Reworks created by T3 enter the next
outer pass at their kind's starting tier. Sequential execution is intentional:
the prototype claimed a global concurrency cap but executed every row serially.
Version 2 states and tests the behavior it actually implements.

## 3. State machine and transactions

```text
pending --claim/lease--> running
running --valid ungated output---------------> committed
running --valid gated output-----------------> awaiting_review
awaiting_review --passing T3 verdict---------> committed
awaiting_review --failing T3 verdict---------> superseded + replacement
running/awaiting_review --bounded exhaustion-> needs_human
```

Worker result persistence and critic creation are one SQLite transaction, so a
crash cannot leave a gated worker marked publishable without its critic job.
Verdict persistence and parent routing are also one transaction. Rejected work
is retained as `superseded` for audit.

Claims use an owner and expiration time. Startup recovers expired claims only;
an unexpired job is not stolen. An OS advisory lock rejects a second dispatcher
for the same database before any work. SQLite uses WAL, foreign keys,
`synchronous=FULL`, explicit `BEGIN IMMEDIATE` transitions, and a 30-second busy
timeout.

## 4. Dependencies and idempotency

`job_dependencies` forms a DAG because an edge can reference only an earlier
job ID. A job is claimable only when all dependencies are `committed`. A failed
or superseded dependency parks the downstream job as `needs_human`; unresolved
dependencies stop the run visibly instead of producing an infinite sleep.

Seed jobs have optional unique idempotency keys. Repeating the exact seed
returns its existing ID. A key reused with different kind or payload fails.
Critic and rework jobs use deterministic internal keys.

## 5. Validation gates

All input and output boundaries are fail-closed:

1. Payloads must be finite JSON, stay within the byte ceiling, use an allowlisted
   kind, contain a task, and contain no secret-bearing key.
2. Evidence URLs must be HTTP(S); source IDs are unique; supplied hashes must
   match exact excerpts.
3. Worker output must contain exactly `RESULT` and `CONFIDENCE`. Confidence must
   be finite and in `[0,1]`.
4. Kind-specific validators enforce dossier claim provenance, viability range,
   scout exclusions, unique names, funding enum, evidence-bound sources, and
   the 12-candidate quota.
5. Critic output must contain exactly the four verdict fields. Risk must be
   finite and in `[0,1]`; issues must be bounded strings; PASS cannot retain a
   critical issue; CONDITIONAL_PASS and FAIL must explain themselves.
6. Code—not prompt prose—applies PASS `<0.30` and CONDITIONAL_PASS `<0.50`.

Malformed T1 worker output escalates once to T2. Malformed terminal output is
retried only within the per-job attempt cap, then parked. A malformed critic can
never commit and parks its parent when exhausted.

## 6. Retry semantics

Two independent caps avoid the prototype's ambiguity:

- `MAX_ATTEMPTS_PER_JOB=3` bounds endpoint, parse, and schema failures for one
  row.
- `MAX_REWORKS_PER_LINEAGE=3` permits an original gated worker plus at most
  three critic-requested replacements.

Rework count is an internal database column, not a user-controlled payload
field. Every replacement links to its predecessor and immutable root. Only the
critic's critical issues enter `retry_brief`.

## 7. Resource containment

Before an external call, the dispatcher checks and reserves:

- per-run call, input-character, and monotonic wall-clock ceilings; and
- persistent per-campaign call and input-character ceilings in SQLite.

The reservation precedes the network call and survives a crash. This may
conservatively count an uncertain call twice, which is safer than undercounting.
If the provider accepted a call just before a crash, lease recovery may issue a
bounded duplicate because the generic endpoints do not guarantee idempotency.
If a ceiling blocks a claimed job, the claim and attempt increment are rolled
back to pending. Output length is bounded by model `max_tokens` and HTTP response
bytes. HTTP calls receive the smaller of request timeout and remaining run time.

These are resource ceilings, not dollar guarantees. A cost ceiling requires a
versioned provider-price table and worst-case preauthorization, which v2 does
not pretend to implement.

## 8. Audit trail

`events` is append-only under normal APIs and records state transitions without
copying full work products or credentials into event details. `runs` records
owner, start/end, stop reason, calls, and input characters. `model_calls`
records the job, tier, model, client class, outcome, input size, and
provider-reported token usage; a crash leaves a conservative `reserved` row.
Each worker and critic result has a SHA-256 integrity hash.

`dispatch.py audit` checks status/kind/tier values, attempts, result hashes,
critic parents and tier, passing review before gated commit, active critics for
awaiting workers, leases, dependency direction, and campaign bounds. It detects
accidental/tampering changes but is not cryptographically tamper-evident because
an administrator who can rewrite SQLite can also rewrite events. External
signed/WORM export is a production extension.

## 9. Stop conditions

A run terminates on queue drain, exact database stop sentinel, run budget,
campaign budget, wall timeout, or no runnable jobs. `needs_human` rows are
terminal and reported; they do not block unrelated runnable work. The stop file
is `<database>.stop`, eliminating current-directory and multi-database ambiguity.

## 10. Unsupported changes requiring a new threat review

- model tools, live search, browser access, or URL fetching;
- multi-host dispatch, NFS/shared-disk SQLite, or bypassing the process lock;
- automatic publication or automatic `needs_human` resolution;
- parallel model calls;
- mutable/evergreen model aliases in a calibrated production campaign; or
- campaign-budget counter reset.
