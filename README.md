# EscrowGuard

A freelance escrow on GenLayer Studionet. The client locks GEN for a job. If the freelancer never delivers, or the client never responds, the money moves on a deadline instead of staying locked. If the client and freelancer disagree, validators read the delivery link and rule who is paid.

## Stages and deadlines

```
AWAITING_FUNDS -> FUNDED -> DELIVERED -> SETTLED
                     |          |
                     |          +-> DISPUTED -> SETTLED
                     |
                     +-> REFUNDED
```

| Step | Who | Method | Happens when |
| --- | --- | --- | --- |
| Lock GEN | client | `fund_escrow` (payable) | stage is AWAITING_FUNDS |
| Deliver | freelancer | `submit_delivery` | stage is FUNDED and before the delivery deadline |
| Approve | client | `approve_release` | stage is DELIVERED |
| Dispute | client | `raise_dispute` | stage is DELIVERED and before the response deadline |
| Ask validators | client or freelancer | `resolve_dispute` | stage is DISPUTED |
| Refund after missed delivery | client | `reclaim_undelivered` | stage is FUNDED and after the delivery deadline |
| Get paid after client silence | freelancer | `claim_unanswered` | stage is DELIVERED and after the response deadline |
| Close a stalled dispute | client or freelancer | `settle_stalled_dispute` | stage is DISPUTED and after the dispute deadline (escrow returns to the client) |

Deadlines are set when each step happens, from the contract's clock. The window length (`window_seconds`) is chosen at deploy time: at least 60 seconds for testing, and days in production.

## Safety

- Every action checks the caller's wallet against the expected role.
- Every action checks the current stage.
- The stage is written before any money is sent, so a payout can happen only once.
- A stalled dispute cannot lock funds forever: after the dispute deadline, `settle_stalled_dispute` returns the escrow to the client.

## Validator ruling

On a dispute, validators fetch the delivery link, compare it with the agreed terms, the delivery note and the complaint, and must agree on `PAY_FREELANCER` or `REFUND_CLIENT`. Reasoning text may differ between validators.

## Tests

`python test_escrow_guard.py` runs 10 tests on the deadline, role, stage and recovery logic, with GenLayer's runtime stubbed.

## Live status

Deployed to Studionet. The live test runs one escrow with a short window to show a deadline firing on-chain. Only a passing live run counts as tested.

## Limits

- Studionet only, not public testnets.
- The escrow holds one job per deployed contract.
- The delivery link is chosen by the freelancer. Validators judge it against the terms, but the link itself is not verified as the freelancer's work.
