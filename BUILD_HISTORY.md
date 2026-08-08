# Build History

Reconstructed from the Aug 5, 2026 working session (Chris Bailey + Claude).

1. **Origin.** Richard Gu asked about the AI-for-professional-services
   sector (Harvey, Legora). Chris uploaded a 57-company market-map xlsx
   for validation and breakdown.
2. **Spot validation.** Harvey $11B round, Legora extension ($600M/$5.6B,
   Atlassian + NVentures, $100M ARR), Tessera $60M a16z round verified;
   Everant found to have zero third-party footprint.
3. **Agent system spec.** Dossier-per-company + 12-candidates-per-category
   scouts; viability rubric excluding funding as an input (the map is a
   capital census — funding ≠ next-year success).
4. **Runtime decision (Chris).** Small traffic-cop operator routing to
   smaller targeted models, minimal concurrency — portable to the
   single-box AI hardware Richard's company will sell. → Ralph Dispatch
   spec + dispatch.py.
5. **Pilot run (critic-gated).** SI category: KaarTech revenue
   contradiction caught, Everant demoted (Chris's decision), Trope
   discovered, displacement-mode taxonomy added (Chris's decision).
6. **Hardening (Chris's directive: don't ship anything that can break out
   and go wild).** Adversarial teardown found six real vectors in the
   first-draft dispatcher — including an unbounded worker↔critic retry
   loop and an infra-error spin that would each have burned tokens
   indefinitely. All fixed with executed tests; see THREAT_MODEL.md.
   PRIOR_ART.md added the same day so no documentation claim outruns
   the citations.
7. **Deliverable.** Workbook v2 with 221-cell audited diff, autofit
   formatting, and full Update_Log trail; sent to Richard.

## Version 2 hardening review — August 7, 2026

8. **Baseline reproduced.** All 11 original tests failed at setup outside the
   author's prior sandbox because they changed directory to the hard-coded
   `/home/claude/ralph-dispatch` path.
9. **State machine corrected.** Added `awaiting_review` and `committed`, atomic
   critic creation/routing, persisted verdicts, strict schemas, finite-number
   guards, immutable retry lineage, result hashes, dependency DAGs, and
   idempotent seeds.
10. **Operational containment added.** Added expiring claims, a single-process
    OS lock, database-specific stop files, persistent campaign ceilings,
    repository-relative prompts, append-only events, v1 migration, and a
    database invariant audit.
11. **Missing runtime pieces completed.** Added Anthropic and OpenAI-compatible
    HTTP clients, a normalized manifest seeder, CLI operations, versioned critic
    prompt, Python 3.11–3.13 CI, and an expanded adversarial test suite.
12. **Claims reconciled.** Removed the unused T0 and unimplemented concurrency
    claims, separated evidence acquisition from tool-less model work, documented
    resource ceilings as non-monetary, and marked the 2026 pilot as a historical
    interactive artifact rather than standalone runtime evidence.

Known limits, stated honestly: software tests use scripted clients and temporary
SQLite databases. No real model has passed the adversarial corpus, no provider
integration or target-hardware endurance run is bundled, no backup/restore drill
has been executed, and default thresholds are uncalibrated. The historical pilot
followed the architecture through interactive tool calls; it is not evidence of
an autonomous end-to-end batch. See `docs/validation_protocol.md` for the gates
that remain before unattended production use.

## 2026-08-08 — Version 2.1 hardening (external review session)

A deep-dive review installed 2.0 clean (41/41 tests, gates green) and then ran
failure probes rather than reading for style. Two probes found HIGH defects:
a 1.1 MB valid dossier burned three paid attempts at the critic gate because
the gate's payload cap was smaller than the worker response cap, and an
invalid API key burned fifteen doomed calls plus fifteen permanent campaign
reservations across a five-job queue while the summary read "queue drained."
Medium/low findings: token-limit truncation invisible, doomed end-of-wall
dispatches with sub-second timeouts, ambient proxy env vars carrying
key-bearing traffic, secret hygiene checking key names but not values, and no
audited export path.

All were fixed in-session as V26–V34 with tests (61 total, coverage 77% on
dispatch.py against the 75% floor). One fix found a bug in its own first
draft: the new `export` CLI command initially created a fresh empty database
when pointed at a missing path; it now refuses like the other
existing-database commands. Design positions defended and kept: sequential
single-dispatcher, char-based budgets as floors under token ledgers, and
fail-closed validation.
