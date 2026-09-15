# VoxDesk — Step 13 evidence schema

Every operational script in the Step 13 toolkit emits evidence in one
consistent shape, and merges it into `scripts/release/evidence.json` without
destroying historical records.

## Machine-readable record (what the scripts emit)

```json
{
  "item_id": "tls-001",
  "status": "PASS | FAIL | BLOCKED | NOT_RUN | SKIPPED | NOT_APPLICABLE",
  "classification": "AUTOMATED | HUMAN | EXTERNAL | INFRASTRUCTURE",
  "severity": "P0 | P1 | P2 | P3",
  "command": "scripts/verify_tls.py --url https://staging.example.com",
  "timestamp": "2026-09-13T00:00:00+00:00",
  "release_commit": "30b91a1a6b59c06334489222ade4511391804b31",
  "environment": "staging",
  "evidence": "TLS verification PASS: scheme=https, expiry_days=88, hsts=True",
  "details": {},
  "verifier": "ops-toolkit"
}
```

`details` is a curated, secret-free dict; raw credentials and customer data
are never written into evidence.

## Registry entry (how it lands in scripts/release/evidence.json)

`merge_evidence_file` converts each record into a registry item:

```json
{
  "item_id": "tls-001",
  "status": "PASS",
  "evidence": "TLS verification PASS: scheme=https, expiry_days=88, hsts=True",
  "verification_date": "2026-09-13",
  "verifier": "ops-toolkit",
  "notes": ""
}
```

Status mapping into the registry vocabulary:

| Script status | Registry status |
|---|---|
| `PASS` | `PASS` |
| `FAIL` | `FAIL` |
| `BLOCKED` | `BLOCKED` |
| `NOT_RUN` | `NOT_RUN` |
| `WAIVED` | `WAIVED` |
| `SKIPPED` | `NOT_RUN` (a skipped check is not success) |
| `NOT_APPLICABLE` | `NOT_RUN` |

## Merge rules (history preservation)

- Adding a record for an item with no existing entry inserts it.
- Re-running with the **same** status and evidence text is idempotent (no
  history churn).
- A changed status or evidence text moves the previous entry into the
  top-level `history` array with `superseded` (item id) and `superseded_by`
  (date) metadata, then replaces the live entry.
- Unrelated top-level keys (`title`, `verified_on`, `step`, …) are preserved.

## What is never in evidence

- Secrets: tokens, keys, passwords, DSNs, `sk_*`/`AKIA*`/`SG.*` values.
- Raw provider credentials or customer PII.
- Fabricated `PASS` results — a missing prerequisite is `BLOCKED` or
  `NOT_RUN`, exactly as observed.

## Authoritative gate

The evidence registry feeds the existing release gate
(`python scripts/release_gate.py`). The toolkit has no second gate algorithm;
it only produces evidence.
