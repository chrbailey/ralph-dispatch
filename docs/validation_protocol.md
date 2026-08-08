# Validation Protocol

This protocol separates software correctness, model calibration, and operating
approval. Passing unit tests is necessary but does not establish research
truth.

## Gate 1 — deterministic software checks

Run from the repository root:

```bash
python -m compileall -q dispatch.py seed.py tests
python -m unittest discover -s tests -v
python seed.py --manifest examples/jobs.example.json --dry-run
```

Reject the build on any failure. CI repeats lint/format checks, compile, tests,
branch coverage (75% aggregate floor), and a clean database audit on Python
3.11, 3.12, and 3.13.

## Gate 2 — manifest and evidence review

For each paid campaign:

1. Validate source licenses and acquisition method outside this runtime.
2. Confirm evidence excerpts preserve enough context to support or refute the
   associated claims.
3. Hash excerpts at acquisition time and validate the manifest with `--dry-run`.
4. Search payloads for personal, confidential, export-controlled, privileged,
   or credential material. The key-name guard is a backstop, not DLP.
5. Assign stable idempotency keys and inspect dependency edges.

## Gate 3 — 10-job calibration

Create a stratified set containing clean cases, missing disclosures,
contradictory numbers, rename/dedupe cases, prompt injection in evidence,
under-quota scout output, unsupported quantitative claims, and at least one
legitimate conditional pass.

Two humans independently grade the gold result and reconcile differences before
model execution. Measure at least:

- critical-error recall and false-pass rate;
- worker schema-valid rate by tier;
- escalation rate and T2 lift over T1;
- critic PASS/CONDITIONAL/FAIL confusion matrix;
- citation support precision;
- rework success rate and calls per committed result; and
- p50/p95 latency plus actual provider token/cost totals.

Do not tune thresholds on the same examples used for the final acceptance
measurement. Freeze model IDs, prompts, thresholds, and evidence hashes for the
campaign. The minimum release bar should be written before viewing results; for
high-stakes external claims, any critical false PASS is a stop condition.

## Gate 4 — supervised canary

Seed a small campaign with deliberately low run and persistent ceilings. Keep a
human present, exercise the exact stop command, simulate one endpoint outage,
restart after an interrupted call, and verify:

- no duplicate committed workers;
- expired-lease recovery remains within attempt caps;
- gated workers never appear committed before the critic;
- campaign reservations match attempted calls;
- `audit` is clean; and
- exported results match stored hashes.

## Gate 5 — adversarial model evaluation

Against the exact deployed critic, test evidence containing nested JSON,
delimiter-like strings, fake verdict objects, instructions to ignore system
rules, Unicode confusables, very long irrelevant passages, source
contradictions, and citations that support only part of a claim. Confirm the
critic has no tools and that invalid output exhausts into `needs_human`.

Model-level prompt injection cannot be proven absent. Record observed attack
success rate and retain human spot-audits of a random PASS sample.

## Gate 6 — release and rollback evidence

Archive, outside the mutable database:

- git commit and clean diff;
- Python and SQLite versions;
- exact model IDs and endpoint provider;
- prompt and manifest hashes;
- test/CI results;
- calibration report and acceptance decision;
- starting and ending budget snapshot;
- final `audit` JSON; and
- a signed or write-once export of runs, events, verdicts, and committed result
  hashes when regulatory or client assurance requires tamper evidence.

Rollback means stop the dispatcher, preserve the database, revert code/prompts
to the frozen version, create a new database or formally migrate forward, and
rerun the canary. Never edit historical committed rows to make a rollback look
clean.
