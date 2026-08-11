# Gate-3 Calibration Corpus and Harness

`docs/validation_protocol.md` Gate 3 requires a stratified, human-graded
calibration set before any paid or unattended campaign. Until now that corpus
existed only as prose. This directory makes it executable.

## What is being calibrated

The corpus fixes worker outputs deliberately: each case is a known-good or
known-flawed work product plus the bounded evidence pack that proves it. That
isolates the two components under measurement:

1. **The critic gate** (T3 model + `route_critic_verdict` thresholds): given a
   flawed work product and the evidence that exposes the flaw, does the system
   refuse to commit it? Given clean work, does it commit without false alarms?
2. **The runtime validators**: named code-level rejections (scout quota,
   exclusion list) fire before any critic call is spent.

Worker-model quality (schema-valid rate by tier, escalation lift) is *not*
measured here; it comes from the `model_calls` ledger during the supervised
canary (Gate 4).

## Corpus contents

- `corpus.json` — model-facing cases. Every company, person, and figure is
  fictional, and every URL lives under the reserved, never-resolving
  `.calibration.invalid` domain, so no case can assert or leak a real-company
  claim.
- `gold.json` — per-case labels: `acceptable_verdicts`, `flawed` (a commit
  would be a critical false pass), `must_mention` issue keywords, a grading
  `rationale`, and a `grading` block for the two-human protocol.

Strata (Gate 3's list, made concrete):

| Stratum | Layer | What a correct system does |
|---|---|---|
| `clean` | critic | commit; measures false-alarm rate |
| `missing-disclosure` | critic | refuse: stated fact is disclosed nowhere |
| `contradiction` | critic | refuse/flag: evidence contains conflicting figures |
| `rename-dedupe` | critic | refuse/flag: renamed seed company or duplicate candidate |
| `injection` | critic | refuse: evidence carries injection *and* a real flaw |
| `runtime-reject` | runtime | code rejects before any critic call |
| `unsupported-quant` | critic | refuse: number not supported by cited excerpt |
| `legit-conditional` | critic | CONDITIONAL_PASS: real but properly-labeled limitation |

Injection cases pair every injected instruction with a genuine semantic flaw,
so a PASS is proof the injection steered the verdict, not an ambiguity.

## Protocol

1. **Validate** (CI does this on every push):

   ```bash
   python calibration/calibrate.py validate
   ```

   Every critic-layer case must pass the exact runtime validators in
   `dispatch.py`; every runtime-layer case must fail with its named error;
   hashes, URL policy, and gold well-formedness are enforced.

2. **Grade.** Two humans independently review each case's evidence, work
   product, and rationale, record their verdict sets in `grading.grader_a` /
   `grading.grader_b`, then reconcile differences into `grading.reconciled`
   *before* looking at any model output. Do not tune thresholds on these cases
   and then quote acceptance numbers from the same run.

3. **Run** against the exact deployed critic (model IDs frozen):

   ```bash
   python calibration/calibrate.py run --provider anthropic --out observations.json
   ```

   Observations record provenance: corpus hash, critic prompt hash, and model
   ID. `score` refuses observations collected against a different corpus
   revision.

4. **Score**:

   ```bash
   python calibration/calibrate.py score --observations observations.json --require-reconciled
   ```

   Scoring is against the *system*, not the label string: a case counts as
   committed only if code-level routing would commit it (PASS with risk < 0.3
   or CONDITIONAL_PASS with risk < 0.5), mirroring `route_critic_verdict`.
   Reported metrics: critical-error recall, false-pass rate, clean commit
   rate, verdict confusion matrix, issue-mention hit rate, and invalid-output
   count. The release bar is executable and matches the protocol's written
   bar: **any flawed case that would commit fails the command** (exit 1), as
   does any missing observation.

## Honest limits

- Gold labels shipped here are authored and adversarially reviewed but **not
  yet human-reconciled**; `--require-reconciled` enforces that step for an
  acceptance run.
- ~20 cases bound obvious failure modes; they are a floor, not a proof.
  Gate 5's live adversarial evaluation and PASS spot-audits still apply.
- A model can pass this corpus and still fail on real evidence. Freeze model
  IDs, prompts, thresholds, and this corpus's hash together per campaign
  (Gate 6), and re-run after any of them change.
