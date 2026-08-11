# Ralph Dispatch

Ralph Dispatch is a bounded, critic-gated research operator for a single host.
One process owns model calls, drains work in model-resident tier batches, moves
low-confidence work upward, and exposes only critic-approved worker results as
`committed`.

The runtime is Python-standard-library only. It supports Anthropic's Messages
API and local/cloud OpenAI-compatible endpoints, but gives models no tools,
shell, live web, credentials, or publishing authority. Research inputs arrive
as bounded, hashed evidence packs.

## What changed in version 2

The original reference implementation established the traffic-cop pattern but
was not safe to run autonomously. Its 11 advertised tests all depended on a
hard-coded `/home/claude/ralph-dispatch` path, worker JSON was not validated,
gated work became `done` before review, critic verdicts were discarded, the
STOP path depended on the launch directory, and two processes could claim the
same job.

Version 2 replaces those implicit behaviors with executable invariants:

- `committed` is a publishable state; gated workers first become
  `awaiting_review` and can reach `committed` only through a schema-valid,
  threshold-valid T3 verdict.
- Worker, scout, dossier, and critic outputs have strict JSON schemas. NaN,
  infinity, out-of-range scores, unexpected keys, duplicate/excluded scout
  names, under-quota scouts, and citations absent from the evidence pack fail
  closed.
- Per-run call/input/wall limits and persistent per-campaign call/input limits
  are reserved before each external call. A crash conservatively keeps the
  reservation.
- SQLite uses WAL, `synchronous=FULL`, foreign keys, explicit transactions,
  expiring claims, v1→v2 migration, idempotency keys, dependency edges, and an
  append-only transition log. A per-call ledger retains model, tier, outcome,
  input size, and provider-reported token usage for calibration.
- An OS advisory lock enforces the documented single-dispatcher assumption.
- Prompts resolve from the repository rather than the current directory. Each
  database has an exact `<database>.stop` sentinel.
- A normalized manifest seeder, operational CLI, database invariant audit,
  multi-version CI, and an adversarial standard-library test suite are included.

## What changed in version 2.1

Version 2.1 is a hardening pass driven by executed failure probes against 2.0,
not hypothetical review. Each fix carries a threat-table entry (V26–V34) and a
test.

- **HTTP error taxonomy.** 401/403 halt the run after one call and return the
  claimed job to `pending` with no attempt penalty; 400/404/413/422 park the
  job immediately; 429/5xx/timeouts retry with exponential full-jitter backoff
  floored by a capped `Retry-After`. Previously an invalid key silently burned
  three attempts per queued job and reported `queue drained`.
- **Critic gate capacity.** The dispatcher-built critic payload embeds the full
  worker result, so its limit is now sized above the worker response ceiling.
  A valid large dossier no longer fails review on size grounds.
- **Truncation detection.** Provider `stop_reason`/`finish_reason` are
  inspected; output cut off by the token limit fails closed as a named
  validation error instead of surfacing as mystery-malformed JSON.
- **Wall-clock floor.** The dispatcher refuses to claim work when the remaining
  run wall cannot honestly serve a call (`MIN_CALL_WALL_SECONDS`), instead of
  dispatching doomed sub-second-timeout requests.
- **Proxy policy.** Ambient `HTTPS_PROXY`/`http_proxy` variables are ignored
  for key-bearing traffic; `RALPH_HTTPS_PROXY` is an explicit opt-in.
- **Secret-value scan.** Payload validation rejects credential-shaped values
  (`sk-ant-…`, AWS `AKIA…`, GitHub `ghp_…`, Slack `xox…`, private-key blocks,
  JWTs), complementing the existing key-name check.
- **Audited export.** `dispatch.py export` re-audits the database and emits
  committed results as JSONL only from a database that passes its own
  invariants; `--allow-dirty` overrides but marks every record
  `audit_clean:false`. Publishing no longer requires raw SQL.
- **Honest stop reasons and visibility.** A drained queue with parked jobs
  reports how many need human review; `run --verbose` streams per-call
  progress to stderr.

## What changed in version 2.2

Version 2.2 closes the operational findings from an external security audit
(V35–V40, each with a threat-table entry and tests) and adds the two operator
commands that audit called missing.

- **Fail-closed numeric parsing.** Oversized integer literals and deeply
  nested JSON in model output previously escaped as `OverflowError` /
  `RecursionError` and crashed the run; both now fail closed as named
  validation errors.
- **Honest call ledger.** A transport-successful call whose output fails
  validation is recorded as `rejected_output`, not `succeeded`, so calibration
  metrics measure usable output.
- **Tamper-evident event log.** Every event hash-chains to its predecessor;
  `audit` verifies the chain and flags edits, insertions, and deletions.
  `status` and `backup` report the chain head for external archival.
- **Transport and file hygiene.** API keys are never sent over cleartext http
  to a non-loopback host; new databases and lock files are owner-only (0600).
- **Budget drift visibility.** `status` reports crash-orphaned reservations so
  conservatively retained headroom loss is visible before it bites.
- **`metrics` and `backup`.** `metrics` computes the validation protocol's
  ledger KPIs (per-tier schema-valid rate, escalations, calls per committed
  worker, rework outcomes). `backup` copies via `VACUUM INTO` and verifies the
  copy (integrity check, row counts, chain head) before trusting it.

The Gate-3 calibration corpus and scoring harness live in `calibration/`.

## State machine

```text
pending -> running -> pending                  retry / tier escalation
                   -> committed                ungated, valid, confident work
                   -> awaiting_review -> committed
                                      -> superseded -> replacement worker
                                      -> needs_human
                   -> needs_human              permanent or exhausted failure
```

`done` is intentionally gone: it mixed “model returned text” with “approved for
use.” `superseded` preserves rejected generations and their event history.

## Quick start

Requires Python 3.11 or newer.

```bash
python -m unittest discover -s tests -v

python dispatch.py --db dispatch.db init
python seed.py --manifest examples/jobs.example.json --dry-run
python seed.py --db dispatch.db --manifest jobs.json
python dispatch.py --db dispatch.db status
```

Run against Anthropic:

```bash
export ANTHROPIC_API_KEY="..."
python dispatch.py --db dispatch.db run \
  --provider anthropic \
  --max-calls 50 \
  --max-wall-seconds 3600
```

Run against a local OpenAI-compatible server:

```bash
python dispatch.py --db dispatch.db run \
  --provider openai \
  --base-url http://127.0.0.1:8000
```

The base URL is the server root; the client appends `/v1/messages` or
`/v1/chat/completions`. Model IDs are pinned defaults and can be overridden with
`RALPH_T1_MODEL`, `RALPH_T2_MODEL`, and `RALPH_T3_MODEL`. Confirm availability
with the provider before a paid run.

After a run:

```bash
python dispatch.py --db dispatch.db audit
python dispatch.py --db dispatch.db metrics
python dispatch.py --db dispatch.db events --limit 100
python dispatch.py --db dispatch.db backup --out backups/dispatch-$(date +%F).db
```

Archive the `chain_head` reported by `status`/`backup` outside the host after
each run; it is the external anchor that makes the event chain tamper-evident
against database rewrites.

Publish or merge only committed worker results after `audit` reports
`"clean": true`.

## Manifest and evidence boundary

`seed.py` accepts a normalized JSON manifest rather than interpreting arbitrary
workbook cells as commands. Each job has a unique `ref`, allowlisted `kind`,
payload, optional idempotency key, and optional dependency refs. Dependencies
are topologically sorted and may reference only earlier database jobs.

Evidence items use this shape:

```json
{
  "source_id": "regulatory-filing-2026",
  "url": "https://example.org/filing",
  "excerpt": "Bounded text fetched by a separate, supervised process.",
  "sha256": "optional SHA-256 of the exact excerpt"
}
```

Credentials and secret-bearing payload keys are rejected. Evidence acquisition
is deliberately outside this runtime: adding a browser/search fetcher changes
the threat model and requires SSRF controls, content-type and size limits,
robots/licensing review, malware isolation, and a separate test campaign.

## Operational controls

```bash
# Exact queue-specific graceful stop
python dispatch.py --db dispatch.db stop

# Remove the stop request; does not start work
python dispatch.py --db dispatch.db resume

# Deliberately raise persistent ceilings; counters never reset in-place
python dispatch.py --db dispatch.db budget --max-calls 7500
```

The stop request is checked before every call. It cannot cancel an in-flight
request; HTTP requests are separately time-bounded. Resource ceilings are not a
dollar guarantee because providers and models price tokens differently.

## Repository layout

- `dispatch.py` — state machine, storage, validation, HTTP clients, CLI
- `seed.py` / `examples/` — normalized, idempotent manifest boundary
- `prompts/` — versioned worker and critic contracts
- `tests/` — containment, integrity, migration, DAG, and seeding tests
- `docs/` — architecture, validation protocol, and research payload spec
- `skill/ralph-dispatch/` — concise agent-facing operating interface
- `THREAT_MODEL.md` — enforced controls and residual risks
- `PRIOR_ART.md` — bounded positioning and primary links
- `results/` — historical pilot artifact, not an autonomous runtime output

## Honest limits

This is a hardened reference implementation, not proof that an LLM critic is
correct. The test suite uses scripted clients; no live model red-team or target
hardware endurance run is bundled. SQLite and the process lock are single-host
designs. The default confidence and critic risk thresholds still require a
hand-graded calibration set. Evidence excerpts can omit decisive context, and
prompt injection remains a residual model-level risk even with JSON data
isolation and no tools.

See `docs/validation_protocol.md` before the first unattended or paid campaign.

## License

MIT. See `LICENSE`. The warranty disclaimer is not boilerplate here: the
validation protocol's remaining gates (real-model calibration, endurance run,
backup drill) are unmet, and the threat model's residual risks apply to any
deployment.
