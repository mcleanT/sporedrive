# Implementation-plan contract (owner standard, 2026-09-22)

Use this when developing, adopting or handing off a substantial or long-running implementation
plan. Routine, well-scoped work can stay with Sol and the executor without the dual checkpoint
panel. Explicit owner model choices and ratified project/scientific gates take precedence; record
the applicable exception. This document defines instruction policy and required handoff content;
it does not add a runtime schema validator, automatic model router or new approval stage.

## Roles and handoff

| Phase / role | Model and effort | Responsibility |
|---|---|---|
| Initial planning | Owner + Astra (`gpt-6-astra`, `xhigh`) + Fable 5.1 (`claude-fable-5-1`, high) | Agree the approach and versioned execution contract. |
| Continuous supervisor | Sol 6 (`gpt-6-sol`, medium) | Drive cmux, collect evidence, manage reservations, reconcile feedback and relay work orders. |
| Implementation executor | Opus 5.5 (`claude-opus-5-5`, applicable session effort policy; medium default) | Sole writer in its worktree; implement, test, repair and finish authorized work. |
| Milestone auditor | Astra (`gpt-6-astra`, `xhigh`), scoped read-only run | Audit correctness, frozen gates and evidence supporting continuation. |
| Planning counterpart | Dedicated Fable 5.1, high | Reassess approach/assumptions and propose a focused delta or no change. |

Owner pins override defaults. Record requested settings separately from observed runtime evidence.
Select the actual models; a prompt naming a role does not switch models. Preserve global settings
unless their change is explicitly authorized. Opus is launched/verified through `executor_session.py`;
Fable has a separate identity and scratch cwd. Sol can inherit the initial Codex task with a compact
handoff; Astra checkpoint audits use bounded review runs rather than the whole supervision history.

## Required plan content

Before the first implementation dispatch, the plan or a linked handoff addendum must contain:

1. **Authority and version:** objective, owner authorization reference, scope/exclusions, plan
   version/hash, relevant scientific definitions and any explicit exceptions.
2. **Roles and routes:** the assignments above, native identities or the concrete binding steps,
   actual model/effort selection evidence, the Sol handoff and separate Astra/Fable response routes.
3. **Acceptance:** observable required behavior, exact canonical checks, required release work and
   evidence sufficient to claim completion. Keep optional improvements in backlog.
4. **Early execution evidence:** for provider/API/pipeline or multistage integration, a bounded real
   entry-point smoke case with output/provenance checks; affected-boundary reruns and representative
   concurrency/failure/resume probes before a large run where applicable. Reserve the allowance.
   Smoke success does not establish scientific quality or replace held-out evaluation gates.
5. **Milestones and holds:** name substantive milestones and the decisive evidence for each; require
   BOTH Astra's audit and Fable's planning response before its dependent next phase. Include the
   early integration smoke result where applicable. Mark independent authorized work that may
   continue while a response is pending. Changed assumptions, repeated failures or unexpected cost
   trigger an additional scoped checkpoint within remaining authorized capacity, never a timer.
6. **Allowances and stop rules:** bounded shared dispatch/review/repair capacity for initial missing
   consultations, each named checkpoint, targeted verification and smoke calls, with owned reviewer
   deadlines and task expiry where applicable. Astra review launches consume review AND work
   allowance; Fable requests consume work allowance. Do not assume ordinary defaults fund an
   arbitrary milestone count. Stop and report the precise limit without self-renewal.
7. **Decision and continuation:** retain both correlated responses, their plan/evidence revision,
   accepted/rejected advice and evidence, unresolved disagreements, the resulting versioned
   decision and one relayed work-order id. Hold only dependent work for missing responses/material
   disagreements. Obtain owner input only for decisions outside authority; in-scope agreed repairs
   and remaining work continue. Finish after required checks and authorized release, then stop.

## Adoption check

The planning lead checks these items locally before declaring a plan execution-ready. Reuse an
already agreed owner/Astra/Fable plan and its consultation evidence. If an older plan lacks a role,
route, allowance or a required consultation, add a compact execution addendum and obtain only the
missing scoped feedback; do not restart planning or rewrite frozen acceptance. Missing required
decisions or capacity blocks the dependent phase, never silently removes a checkpoint. Report
unavailable counterparts as pending, not consulted.

During execution Sol uses [the driver protocol](driver-protocol.md#planning-checkpoints-sol-drives-astra-and-fable-advise-owner-rule-2026-09-22)
and [the checkpoint packet](checkpoint-packet.md). Both responses are required at each declared
checkpoint; a Fable plan delta cannot stand in for an Astra review, nor an Astra verdict for Fable
feedback. Reconcile disagreements into one work order. Additional clarification/review needs
remaining allocated capacity; neither adviser grants new scope or resets a budget.
