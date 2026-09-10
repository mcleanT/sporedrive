# Integration readiness before expensive acceptance

Read this before a task's first live acceptance cycle against a real provider or a
representative multi-stage runtime path (initial call -> parse/store -> downstream
reaction -> final result). **It does not apply to documentation edits or ordinary local
code changes** — those need no phase declaration and no reserved call allowance. The
point is narrow: stop basic harness defects (bad request shape, an unenforced provider
option, a broken multi-stage transition, a truncation that a diagnostic waves through)
from being discovered for the first time inside a strict, expensive scientific
acceptance cycle instead of a cheap technical one.

## Three prospectively-defined phases

Declare which phase a call belongs to before making it; do not relabel afterward.

1. **Technical debugging.** A small, explicitly reserved call allowance against synthetic
   or development cases, including valid and negative controls, run on the actual
   deployed path (not a mock that bypasses the defect). Every attempt — pass or fail —
   stays in the ledger; none is dropped for having been "just a smoke test." Preserve
   useful raw responses for offline replay so a parser/validator fix can be re-checked
   without another live call. A smoke call that causes an implementation change is
   debugging evidence, not independent acceptance evidence — it cannot also count toward
   the acceptance cycle it motivated.
2. **Frozen acceptance.** Freeze the task's relevant provider settings, prompt/schema,
   semantic adapters, parser and gate configuration, then run the required fresh
   acceptance checks under their own allowance. Do not patch the implementation partway
   through a cycle and blend responses gathered before and after the change into one
   passing result. Passing technical acceptance establishes that the harness works, not
   that the science is good or generalizes.
3. **Scientific/production execution.** Once required acceptance passes, launch the
   already-authorized work immediately — do not insert another ad hoc review cycle.
   Preserve the frozen scientific definitions, the full intended evaluation, existing
   retry rules and reporting boundaries; this phase does not reopen or renegotiate them.

For a study whose already-approved contract counts smoke calls inside acceptance,
preserve that contract until an explicit prospective amendment. Never relabel a past
failure, transfer spent calls between components after the fact, relax a threshold, tune
on held-out outcomes, or build a new denominator from only the successful historical
calls.

## Boundary -> required evidence

| Boundary | Required evidence when relevant |
|---|---|
| Request construction | Inspect the serialized request actually sent: schema, mutually exclusive branches, required fields, list bounds and dynamic allowed IDs agree with the validator. |
| Provider support | Verify the exact requested option is enforced on the actual endpoint/backend; a supported upstream feature or an HTTP 200 alone is insufficient. |
| Representative multi-stage path | Complete initial output -> parse/store -> discussion/reaction -> final result, with real visibility/ID restrictions. Include the actual transition between stages and its client/event-loop lifetime. |
| Failure and recovery | Exercise a bounded injected failure locally and, where necessary, a real retry/resume. Unknown outcomes stay distinct from failures; no duplicate dispatch on resume. |
| Acceptance evaluator | Scope requests/results to the same cycle/build, and test that malformed, truncated, wrong-schema or error outputs cannot pass the diagnostic. |

After a demonstrated failure, inspect the same mechanism across the bounded related
surface once — e.g. an allowed-ID bug warrants checking the other identifier fields in
the same request contract, not an unrelated repository audit. Replay retained bodies
offline for parser/validator repairs where that is valid; use a fresh live probe only
when request semantics, provider handling or runtime lifetime actually changed. A broad
acceptance rerun follows the owning study's contract, not a ritual triggered by a
cosmetic edit (a README change with no code/config difference does not retrigger it).

## Adapter enforcement rule

An adapter may claim **managed call enforcement** only if all of the following hold:

- It reserves each live call (or an explicitly fixed batch) *before* issuing it, with
  phase and freeze identity attached to the reservation.
- Freeze identity is computed from the actual relevant implementation/configuration at
  reservation time — never a copied or hand-maintained constant.
- If a reserved batch can span a change, the adapter rechecks freeze identity before
  each issue in that batch; a mismatch blocks the remaining dispatch in the batch and
  invalidates the current acceptance attempt (it does not silently fall back to the old
  identity).
- Settlement binds each response to its reservation; an unknown/never-settled outcome
  remains charged, never quietly dropped or refunded.

An adapter that only reports call totals after the fact is explicitly **observational**
and must not claim enforced phase or call limits — say so rather than implying a
guarantee the adapter cannot back.

## What SporeDrive stores vs. what study code supplies

SporeDrive's generic core stores phase, allowances, readiness references and freeze
identity. Study-specific code supplies the actual calls and the scientific pass/fail
gates. Do not import a study's request/response schema, a provider-specific parsing
regex, a numeric acceptance threshold, or a study's cycle counts into generic
SporeDrive core — those stay in the owning study's adapter, which implements this
contract against its own provider and gates. Exercise the generic handshake itself with
fake-provider fixtures, not by retrofitting a currently running study.
