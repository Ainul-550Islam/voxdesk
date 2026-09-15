# VoxDesk — Pricing & plans

## Tiers

| | Free (trial) | Starter | Growth | Scale |
|---|---|---|---|---|
| Seats | 1 | 3 | 10 | unlimited |
| Minutes/mo | 30 | 500 | 2,500 | custom |
| Numbers | 0 (rental) | 1 | 3 | 10 |
| Integrations | HubSpot/Google | HubSpot, Google, Pipedrive, Zoho | all | all + white-label |
| SMS campaigns | — | ✓ | ✓ | ✓ |
| Analytics | basic | basic | advanced | advanced + exports |
| Support | docs | email | priority email | dedicated |

## Usage billing

Beyond included minutes: per-minute overage on every paid tier, computed from
the call ledger (`app/billing/reconciliation.py`), with transparent line items
of model + telephony cost. The reconciliation report flags any discrepancy
between the ledger and the provider bill — it never silently absorbs one.

## Trials

- The `trial` plan (`POST /api/billing/checkout` with `plan_code: "trial"`)
  starts a 14-day trial — no card required, $0 price.
- Trials are **hard-capped**: `overage_enabled=false`, so exceeding the
  allowance stops the action rather than metering an overage bill.
- Trials auto-convert or expire; expired trials fall back to Free.

## Upgrades & downgrades

- **Upgrade** — immediate, prorated: you pay the difference for the remaining
  cycle.
- **Downgrade** — effective at the end of the current cycle; you keep your
  higher tier until then (no proration clawback).

## Self-hosted / white-label

License seats are issued as **HMAC-signed tokens** (`app/billing/licensing.py`,
`/licenses/...` endpoints). The public-verification design means your on-prem
install verifies licenses offline; seats and expiry are enforced at check time.

## Notes

Prices, overage rates and exact feature boundaries are business decisions made
in the billing table — this document is the plan definition; the code enforces
tier entitlements (seats, minute caps, integration flags).
