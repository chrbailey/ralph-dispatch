# AI-Native Services Research Agent System — Build Spec
**Prepared for:** Chris Bailey, ERP Access Inc. · **Origin:** Richard Gu inquiry · **Seed universe:** AI_Native_Services_Market_Map_Aug_2026 (57 companies, 17 categories)
**Date:** August 5, 2026

---

## 1. Architecture

Three logical worker stages plus a QA gate, modeled on the Critic Loop pattern
(Worker → Critic → Ralph). These are queue roles, not concurrently autonomous
agents with tools:

```
ORCHESTRATOR
├── DOSSIER AGENTS  (57 instances — one per seed company)      [Worker role]
├── SCOUT AGENTS    (17 instances — one per category;           [Worker role]
│                    each must surface ≥12 candidates NOT in seed list)
├── CRITIC GATE     (independent; never sees worker prompts)    [Critic role]
└── MERGE/ROUTER    (dedupe, score, route retries)              [Ralph role]
```

**Run order:** A supervised acquisition process first builds bounded evidence
packs. Ralph Dispatch then drains T1 extraction, T2 synthesis, and T3 criticism
sequentially in model-resident batches. Every assembled dossier and scout list
passes the critic before entering the master file. The critic checks claims
against supplied evidence; it has no live web or tools and must not claim
independent retrieval. Fabricated, unsupported, or source-mismatched numbers
fail. Three critic-requested reworks escalate to a human.

**Ground rules (baked into every agent prompt):**
- Never invent a number. "Not disclosed" is a valid and preferred answer.
- Every quantitative claim carries a source URL and a confidence tag (High/Medium/Low) matching the workbook's convention.
- Distinguish company-disclosed vs. third-party-estimated vs. journalist-reported.
- Flag press-release-only companies explicitly (see Everant problem, §5).

---

## 2. Dossier Agent — Domain Requirements

The block below preserves the research schema. Executable JSON contracts live
in `prompts/dossier_section_system.md` and
`prompts/dossier_assemble_system.md`; do not copy this prose into runtime code.

```
ROLE: You are a diligence researcher producing a single-company dossier
for an AI-native services market map. Correctness beats completeness.
Never fabricate; mark gaps as NOT DISCLOSED.

COMPANY: {company_name}
CATEGORY: {category}
SEED DATA (verify, don't trust): {workbook_row}

DELIVER THIS SCHEMA:

WHO
- Founders (names, prior roles, prior exits)
- Key executives and notable hires in last 12 months
- Investors by round, with lead investors flagged
- Named customers (company-confirmed vs. reported)

WHEN
- Founding date; pivot/rename history (e.g., Legora ex-Leya ex-Judilica;
  Onshore ex-SPRX; Bretton ex-Greenlite)
- Full funding timeline with dates, amounts, valuations
- M&A events (acquired, acquirer, or acquiree)

WHERE
- HQ, offices, target geography, remote/onsite delivery model

WHY
- The specific labor budget it displaces (name the line item)
- Pricing model: seats / usage / fixed-fee outcome / contingency
- Who holds liability when the work product is wrong

HOW
- Delivery mechanics: pure software, agent + human review, embedded teams
- Human hours per unit of delivered work (estimate + basis)
- Platform dependencies (which foundation model, which enterprise
  platform it writes to, what breaks if that access is revoked)

INTERRELATIONS
- Shared investors with other seed companies
- Direct competitors inside and outside the seed list
- Acquisition exposure: who could buy it; who it could buy
- Platform-vendor absorption risk (SAP/Salesforce/Oracle/ServiceNow/
  Microsoft native features that overlap)

VIABILITY SCORE (see §4 rubric) with one-paragraph justification.

OUTPUT: markdown, ≤600 words, every claim sourced.
```

---

## 3. Scout Agent — Acquisition and Domain Requirements

The hunting grounds below direct the supervised evidence-acquisition stage.
The runtime scout has no browser and may use only the evidence pack. Its
executable JSON contract is `prompts/scout_system.md`.

```
ROLE: You are a deal-sourcing scout. Your job is to find companies the
funded-company press cycle MISSED. A famous company is a failed find.

CATEGORY: {category}
EXCLUSION LIST (already mapped — do not return these): {seed_companies}

QUOTA: minimum 12 qualified candidates not on the exclusion list.

HUNTING GROUNDS — search these in order:
1. YC batch pages (W25, S25, W26, S26) filtered by category keywords
2. LinkedIn: companies <200 employees whose tagline claims outcome
   delivery in {category}; check headcount growth trend
3. Trade press for the CATEGORY, not tech press (e.g., Accounting Today,
   HFMA, Claims Journal, SIA staffing reports, ASUG/SAPinsider for ERP)
4. Incumbent product pages: what agentic products have existing service
   firms in this category shipped? (the KTern pattern)
5. Non-US: EU-Startups, Sifted, Tech.eu, Tracxn India, e27/Tech in Asia
6. Bootstrapped signal: conference speaker lists, partner directories
   (Salesforce AppExchange, SAP Store, ServiceNow Store), podcast guests
7. Job boards: companies hiring "forward-deployed engineers" or
   "AI delivery leads" in this category

QUALIFICATION BAR (all three required):
a. Plausibly captures LABOR/services budget, not just SaaS budget
b. Evidence of real operations: named customer, partner listing,
   verifiable team, or shipped product — a landing page alone fails
c. Not a feature of a horizontal platform

PER CANDIDATE RETURN: name, URL, one-line thesis fit, funding status
(funded / bootstrapped / unknown), strongest evidence of real revenue,
and why the press-driven map missed it.
```

---

## 4. Next-Year Viability Rubric (0–100)

| Factor | Weight | What scores high |
|---|---|---|
| Outsourcing wedge | 25 | Existing outsourced budget line to displace; buyer already writes checks for this work |
| Outcome liability | 20 | Fixed-fee/contingency pricing; firm holds professional liability (Crosby, Norm Law model) |
| Execution access | 15 | Write access to system of record; reusable implementation memory; process ontology |
| Capital-to-traction ratio | 15 | Revenue per dollar raised; penalize priced-for-perfection multiples (>40x ARR) |
| Platform absorption risk | 15 | Inverted — high score = low risk of SAP/Salesforce/ServiceNow shipping it natively |
| Category crowding | 10 | Inverted — penalize 4+ funded players sharing one budget line |

**Explicit bias correction:** funding amount and valuation are NOT scoring inputs. They enter only through capital-to-traction and procurement-viability floors.

---

## 5. Known Data Hazards (Critic checklist)

1. **The Everant problem.** The workbook's reference model for agent-first SIs has no public funding, press, or named customers. Any candidate whose only evidence is its own website gets flagged `SELF-REPORTED ONLY`.
2. **Stale rounds.** Legora is already outdated in the seed file ($600M total / $5.6B post after April extension, Atlassian + NVentures added, $100M ARR crossed). Assume every row ≥3 months old has moved.
3. **Rename chains.** Onshore/SPRX, Bretton/Greenlite, Legora/Leya/Judilica — dedupe on prior names or scouts will "discover" seed companies.
4. **Headcount ambiguity.** Lawhive's "450 lawyers," Mercor's "30,000 contractors," Crescendo's agent workforce — employees ≠ contractors ≠ network. Record the counting basis.
5. **ARR theater.** "Contracted TCV," "annualized run rate," and "revenue" are three different numbers. Unframe's $100M is TCV, not ARR. Label which one.

---

## 6. Category Roster — 17 Scouts with Exclusion Lists & Hunting Notes

| # | Category | Exclusion list (seed) | Scout-specific notes |
|---|---|---|---|
| 1 | Insurance brokerage | WithCoverage, Harper | Only 2 seeds vs. the thesis's largest TAM — thinnest coverage on the map. Hunt wholesale/E&S brokers, benefits brokerage, MGA automation |
| 2 | Accounting / audit | Basis, Rillet, Puzzle, Digits, Accrual | Hunt audit-specific (workpaper automation), CAS-in-a-box firms, offshore-replacement plays |
| 3 | Healthcare revenue cycle | Commure, Anterior, Tennr, SmarterDx, CodaMetrix | Hunt denials-management specialists, credentialing, patient-access; HFMA vendor lists |
| 4 | Claims adjusting | EvolutionIQ, Sprout.ai | 2 seeds only. Hunt TPA-replacement, field-adjusting automation, subrogation AI |
| 5 | Tax advisory | Blue J, TaxGPT, Ravical, Onshore | Hunt SALT, transfer pricing, indirect tax/VAT — high-fee niches with no seed |
| 6 | Legal / transactional | Harvey, Legora, Crosby, Lawhive | Crowded at top; hunt IP prosecution, immigration, e-discovery-as-outcome, ALSPs going agentic |
| 7 | Regulatory / compliance | Norm Ai | 1 seed. Hunt bank exam prep, pharma regulatory affairs, environmental compliance |
| 8 | IT managed services | Serval, Edra, Moveworks, Dropzone AI | Hunt MSP-native agents, NOC automation, ITAM; MSP trade press (ChannelE2E, CRN MSP lists) |
| 9 | Supply chain / procurement | Lio, Magentic, Tacto, Pactum | Hunt freight audit, customs brokerage, supplier onboarding/compliance |
| 10 | Recruitment / staffing | Mercor, Juicebox, Jack & Jill, ConverzAI | Hunt healthcare staffing, light-industrial high-volume, RPO-replacement |
| 11 | Mgmt consulting / AI deployment | Distyl, Invisible, Unframe, Ciridae | Hunt PE portfolio-ops specialists, restructuring/turnaround AI, pricing consultancy replacement |
| 12 | Customer ops / BPO | Sierra, Decagon, Maven AGI, Crescendo | Over-crowded at enterprise; hunt vertical BPO (healthcare call centers, utility ops, collections) |
| 13 | Financial services ops | Rogo, Hebbia, Bretton AI | Hunt fund admin, loan ops, insurance ops, trade settlement |
| 14 | Software eng / modernization | Blitzy | 1 seed. Hunt mainframe/COBOL specialists, test-automation-as-outcome, offshore-dev replacement |
| 15 | Enterprise app implementation / SI | Everant, Tessera Labs, Luzid, KTern.AI, SASA/AiFA, Cirra AI, SRE.ai | **Chris's lane.** Hunt Workday/Oracle/NetSuite/ServiceNow equivalents of the SAP/SFDC seeds; SAP Store + AppExchange agentic listings; incumbent SI internal tools going productized |
| 16 | Marketing / GTM ops | Gradial, ColdIQ | Hunt SEO-agency replacement, paid-media ops, PR/comms automation |
| 17 | Enterprise ops / back office | Convey | 1 seed. Hunt AP/AR-as-outcome, payroll ops, facilities/lease admin |

### Missing categories — spin up 5 additional scouts (no seeds exist)

| # | New category | Rationale |
|---|---|---|
| 18 | Real estate services / property mgmt | Leasing, lease abstraction, property accounting, CAM reconciliation — large outsourced budgets, zero seeds. **Direct overlap with the GU platform; Chris has proprietary visibility here** |
| 19 | Government / GovCon services | Proposal writing, compliance (FAR/DFARS), contract admin — **SDVOSB lane; Chris has channel advantage** |
| 20 | Insurance underwriting | Distinct from brokerage and claims; submission intake, risk scoring, quote generation |
| 21 | AEC / engineering services | Permitting, code compliance, construction admin, takeoffs |
| 22 | Wealth & fund operations | RIA back office, fund accounting, investor reporting, K-1 prep |

---

## 7. Execution Plan

1. **Phase 1:** Approve the schema, evidence policy, and acceptance metrics in
   `validation_protocol.md`.
2. **Phase 2:** Acquire and review evidence packs for 22 categories; normalize
   them into an idempotent manifest.
3. **Phase 3:** Run a 10-job calibration, then a supervised low-budget canary.
4. **Phase 4:** Run scouts and dossiers through Ralph Dispatch; require a clean
   database audit and human review of every `needs_human` row.
5. **Phase 5:** Merge committed workers into the workbook, preserving source
   qualifiers, result hashes, model/prompt versions, and the Update_Log trail.

**Honest caveat:** The campaign is 100+ research runs plus evidence acquisition.
Resource ceilings bound the dispatcher but do not establish a dollar budget or
research correctness. Freeze model IDs and measure actual provider usage during
the canary before approving the full run.
