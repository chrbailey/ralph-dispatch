# Threat Model and Containment Analysis

## Scope and assets

Ralph Dispatch protects model-call resources, queue integrity, research
provenance, critic independence, and the boundary between generated text and a
publishable result. It assumes one trusted host administrator, one dispatcher
process per database, and untrusted model/evidence text.

The runtime does not protect a host already controlled by an attacker, an
administrator who rewrites SQLite and its event log, a malicious provider, or
evidence acquisition performed outside the process.

## Trust boundaries

1. **Manifest → queue:** allowlisted kinds, finite/size-bounded JSON, rejected
   secret-bearing keys, validated evidence records, idempotency, DAG edges.
2. **Queue → model:** repository-pinned prompts plus JSON data; no tools,
   credentials, database, filesystem, retrieval, or publish capability.
3. **Model → queue:** byte bounds, exact schemas, finite numeric ranges,
   kind-specific validation, attempt caps, integrity hashes.
4. **Worker → critic:** task, evidence, confidence, result hash, and work
   product only; no worker system prompt or hidden reasoning.
5. **Queue → downstream use:** only committed worker rows after a clean audit.

## Original vectors retained and strengthened

| ID | Vector | Version 2 control | Executable evidence |
|---|---|---|---|
| V1 | Infinite worker↔critic chain | Internal `rework_count`, immutable root/predecessor links, at most three replacements | `test_V1_*` |
| V2 | Endpoint-error spin | Three claimed attempts per row; final failure parks; no inner unbounded retry | `test_V2_*` |
| V3 | Unbounded calls/time | Pre-call run call/input/monotonic-wall checks plus HTTP timeout | `test_V3_*` |
| V4 | No graceful stop | Exact `<database>.stop` checked before every call | `test_V4_*` |
| V5 | Orphaned running rows | Expiring owner leases; only stale claims recover; cap-exhausted claims park | `test_V5_*` |
| V6 | Kind path traversal | Kind allowlist and fixed absolute prompt map | `test_V6_*` |

## Additional vectors found in this review

| ID | Vector in the prototype | Version 2 control | Executable evidence |
|---|---|---|---|
| V7 | All containment tests were pinned to `/home/claude/ralph-dispatch` and failed elsewhere | Tests and prompts are repository-relative and run from arbitrary CWD | full suite, `test_V6_*` |
| V8 | Worker result became `done` before critic approval; downstream readers could publish it | New `awaiting_review` state; only a passing route transaction writes `committed` | `test_G8_*`, `test_G9_*` |
| V9 | Worker JSON and confidence were not parsed; NaN made `< floor` false and silently passed | Exact envelope, kind schemas, finite `[0,1]` validation | `test_G6_*`, `test_G11_*` |
| V10 | Critic accepted any JSON object, including missing fields, invalid risk, or contradictory PASS | Exact schema, bounded issue lists, semantic verdict checks, finite risk | `test_G2_*`, `test_G7_*` |
| V11 | Critic verdict and issues were discarded | Canonical verdict, hash, parent `review_result`, and events persist atomically | `test_G9_*` |
| V12 | Two dispatchers could select the same pending rows and duplicate paid calls | Non-blocking OS lock before DB recovery or calls; atomic claims remain defensive | `test_V7_*` |
| V13 | STOP resolved against the process CWD, not “next to the DB” as documented | Sentinel derives from the resolved database path | `test_V4_*` |
| V14 | Per-run budget reset allowed an unattended cron loop to spend forever across runs | Persistent campaign reservations in SQLite; no usage-reset command | `test_V8_*` |
| V15 | User payload controlled the lineage retry counter | Rework count moved to an internal DB column and is never accepted from payload | `test_V1_*` |
| V16 | Payloads could contain credentials or arbitrarily large prompt data | Secret-key guard; payload, prompt, response, and event byte ceilings | `test_V9_*`, `test_V10_*` |
| V17 | XML-like work-product delimiters could be closed by injected text | Entire critic input is JSON-encoded data under an explicit untrusted-data contract | `test_G4_*` |
| V18 | “12 candidates” and source discipline existed only in prose | Runtime enforces quota, dedupe, exclusions, enums, HTTP URLs, and evidence binding | `test_G11_*` |
| V19 | Declared dossier sections had no executable dependency/assembly relationship | Database DAG, committed-dependency claim rule, injected hashed dependency results | `test_G12_*`, seed tests |
| V20 | Retrying a seed could duplicate a campaign | Unique idempotency keys and manifest refs | idempotency and seed rerun tests |
| V21 | A crash between status updates could split the critic gate | Explicit transactions cover worker→critic and verdict→route transitions | gate integration tests |
| V22 | Result tampering had no detection surface | Canonical result SHA-256 plus invariant audit | `test_G10_*` |
| V23 | Missing/corrupt prompt or payload could leave a job `running` until restart | Permanent boundary errors park with sanitized cause and event | dispatch path tests |
| V24 | The loop slept forever on dependency-blocked work | Failed dependencies park; unresolved DAG state stops visibly | DAG and run tests |
| V25 | Provider token usage was returned and then discarded | Per-call ledger stores model, tier, outcome, input size, and reported input/output tokens | `test_G14_*` |
| V26 | Critic gate capped below worker output capacity, so valid large results burned three paid attempts and parked | `MAX_CRITIC_PAYLOAD_BYTES` sized to hold a maximal worker result plus review scaffolding; `validate_payload` selects the limit by kind | `test_V26_*` |
| V27 | An invalid API key drained the full attempt budget for every queued job while reporting "queue drained" | HTTP failures are classified; 401/403 raise `ModelEndpointFatalError`, which unclaims the job without an attempt penalty and halts the run after one call | `test_V27_*`, `TestHttpErrorTaxonomy` |
| V28 | Deterministic request rejections (400/404/413/422) were retried as if transient | `ModelCallPermanentError` parks the job after one attempt with a `model_call_rejected` event | `test_V28_*` |
| V29 | 429/5xx retries used a fixed 5-second sleep and ignored `Retry-After` | Transient errors back off exponentially with full jitter, floored by the provider's capped `Retry-After`, still bounded by the per-job attempt cap | `test_V29_*`, backoff unit test |
| V30 | End-of-wall dispatch issued doomed calls with sub-second timeouts | `MIN_CALL_WALL_SECONDS` floor refuses to claim work the remaining wall cannot honestly serve; the job stays pending with zero attempts | `test_V30_*` |
| V31 | Secret hygiene checked key names only; credential-shaped values (`sk-ant-…`, `AKIA…`, `ghp_…`, private-key blocks) passed validation | Recursive value scan rejects credential-shaped strings anywhere in a payload | `test_V31_*` |
| V32 | Token-limit truncation was invisible; truncated output could reach validation looking merely malformed | `stop_reason`/`finish_reason` are inspected; truncation fails closed as a named `ValidationError` | `TestTruncationDetection` |
| V33 | Ambient `HTTPS_PROXY` environment variables silently routed key-bearing traffic through unaudited proxies | Openers ignore ambient proxy variables; `RALPH_HTTPS_PROXY` is the only, explicit, opt-in | `TestProxyPolicy` |
| V34 | Publishing required raw SQL against the database, bypassing every invariant | `export` re-audits the database and refuses to emit from one that violates its own invariants unless `--allow-dirty` is passed, which marks records `audit_clean:false` | export tests |

## Prompt-injection analysis

Evidence and model output remain adversarial. JSON encoding prevents an attacker
from syntactically terminating a bespoke `<work_product>` region, but it does
not create a perfect instruction/data security boundary inside an LLM. The
stronger controls are architectural: no tools, no secrets, no retrieval, no
publish authority, exact output schema, evidence-bound citations, never-lower
critic tier, bounded retries, and required human handling of exhausted cases.

Before production, run the adversarial corpus in
`docs/validation_protocol.md` against the exact critic model. Spot-audit a
random sample of PASS verdicts. A live search tool, URL fetcher, browser, shell,
or publishing connector is a material scope change and invalidates this review.

## Residual risks

1. **Model false PASS.** A schema-valid critic can still reason incorrectly or
   follow injected content. Calibration and human sampling are mandatory.
2. **Evidence omission.** Hashes prove exact excerpt integrity, not that the
   excerpt is complete, licensed, authentic, or representative.
3. **Administrative tampering.** The audit log is append-only by application
   convention, not WORM. Export and sign it externally for assurance use.
4. **Provider privacy and availability.** Payload text leaves the host for a
   cloud provider. Data-processing terms, retention, residency, and endpoint
   authentication remain deployment responsibilities.
5. **Budget unit.** Calls and characters bound activity but are not a monetary
   ceiling. Prices and tokenizer behavior differ by provider/model.
6. **Timeout cooperation.** The included HTTP client is time-bounded. A custom
   client must honor the supplied timeout or the wall ceiling becomes a
   boundary-only check.
7. **Single host.** `fcntl` and SQLite are not a multi-host lease service. NFS
   and shared-disk deployments are unsupported.
8. **Disk failure/full disk.** SQLite durability reduces but cannot eliminate
   filesystem, controller, or media failure. Backup and restore are untested in
   this repository.
9. **Secret-value scan is heuristic.** The V31 patterns catch common credential
   shapes, not every secret. High-entropy values without a recognizable prefix
   pass. The key-name check and the pattern scan are complementary layers, not
   proof of absence.
10. **Error classification trusts status codes.** A provider that returns 200
    with an error body, or 429 for a permanent condition, will be routed by the
    code it chose, not the condition it meant.
9. **Sensitive content.** Key-name rejection is not semantic DLP. A secret can
   appear under an innocent key or inside an excerpt.
10. **Human process.** Direct SQL can violate every state invariant. Restrict
    database write access and do not “fix” `needs_human` rows manually.
11. **Ambiguous external-call completion.** A crash after the provider accepts a
    request but before the job transition can cause a bounded duplicate call on
    lease recovery. The conservative call reservation remains in the ledger;
    exactly-once model execution requires provider idempotency support that the
    two generic protocols do not guarantee.

## Validation status

The bundled tests use scripted clients and temporary databases. They validate
software transitions and containment, not research accuracy, provider behavior,
live injection resistance, backup recovery, target-hardware endurance, or cost
calibration. Those gates remain explicitly pre-production work.
