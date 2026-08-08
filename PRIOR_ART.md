# Prior Art and Positioning

Ralph Dispatch combines established patterns. This document bounds its claims;
it does not assert a new routing algorithm, workflow framework, database, or
critic method. Links were rechecked August 7, 2026.

## Model routing and cascades

- [FrugalGPT (Chen, Zaharia, and Zou, 2023)](https://arxiv.org/abs/2305.05176)
  learns LLM cascades that trade cost and quality. Its reported “up to 98%” cost
  result is benchmark-specific, not a deployment guarantee.
- [RouteLLM (Ong et al., ICLR 2025)](https://github.com/lm-sys/RouteLLM)
  learns routers from preference data. Its official repository reports up to
  85% savings at 95% GPT-4 performance on its benchmarks; those figures do not
  predict Ralph's corpus.
- [Dynamic Model Routing and Cascading for Efficient LLM Inference: A Survey
  (Moslem and Kelleher, 2026)](https://arxiv.org/abs/2603.04445) organizes the
  field by decision timing, information, and computation. Ralph is a
  deterministic, post-response cascade with confidence/schema escalation—not a
  learned router.

Version 2 removed the unused T0 classifier. Known job kinds map deterministically
to a starting tier, and T1 failures may promote to T2. This makes the system
auditable but less adaptive than learned routing.

## Durable orchestration

- [LangGraph's official persistence documentation](https://docs.langchain.com/oss/python/langgraph/persistence)
  describes per-step checkpoints, fault-tolerant execution, state history, and
  human-in-the-loop workflows.
- [Python's sqlite3 transaction documentation](https://docs.python.org/3/library/sqlite3.html#transaction-control)
  documents the transaction behavior on which this implementation relies.

Ralph's SQLite state machine is a narrow single-host alternative for a bounded
batch, not a competitor to general workflow runtimes. It lacks distributed
leases, multi-host failover, workflow versioning, compensation, hosted
observability, and a production control plane.

## Critic and reflective loops

- [Reflexion (Shinn et al., 2023)](https://arxiv.org/abs/2303.11366) uses
  linguistic feedback and retry memory for language agents.
- LLM-as-judge, self-refine, and worker→critic→revise loops are broadly used.
  A separate critic model does not establish truth and can share worker biases.

Ralph's contributions here are engineering constraints: the critic receives a
reduced JSON data view, never the worker system prompt; T3 is not a worker
escalation target; exact verdict schemas and numeric thresholds are applied by
code; gated work is not committed before approval; and exhausted review paths
require a human. These are disciplined controls, not research novelty.

## Positioning that survives scrutiny

1. **Deployment shape.** Sequential tier-batch draining reduces model swaps on
   fixed-memory hardware. The performance benefit remains a hypothesis until
   benchmarked on the target box with real model-loading telemetry.
2. **Audit density.** Routing, durable local state, gates, budgets, evidence
   binding, and containment live in a small standard-library implementation.
   That makes review tractable; it does not make the implementation a full
   workflow platform.
3. **Domain payload.** The service-market taxonomy, evidence policy, viability
   rubric, prompts, calibration corpus, and human decisions are more defensible
   assets than the dispatcher code.
4. **Fail-closed contract.** Version 2's meaningful improvement is the explicit
   state/validation boundary between generated text and committed work. This is
   an implementation property backed by tests, not a claim that LLM judgment is
   reliable in the abstract.
