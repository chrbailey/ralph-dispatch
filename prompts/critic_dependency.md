# Critic Tier — Dependency Decision

The original prototype described an external `critic-loop` skill while the
runtime actually hard-coded a different critic prompt. That was neither
reproducible nor auditable.

Version 2 vendors the executable contract in `prompts/critic_system.md` and
tests its hashable, repository-relative loading path. The behavioral pattern is
still derived from the external critic-loop work, but a deployed dispatcher no
longer depends on an ambient file under a particular user's home directory.
`load_prompt()` passes the critic only a task, evidence pack, worker confidence,
result hash, and work product—never worker system prompts or hidden reasoning.
