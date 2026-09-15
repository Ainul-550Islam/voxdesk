"""Evidence registry loader (Step 11).

The evidence registry is the record of what has actually happened, item by
item. Its JSON shape is:

    {"items": [{"item_id", "status", "evidence", "verification_date",
                "verifier", "notes", "waiver": {"reason","approver","date",
                "expiry"}}]}

An item with no entry — or a status not present in the file — is treated by
the gate as NOT_RUN, never as PASS. This module only reads and validates; the
gate interprets.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.release.models import Evidence, Status, Waiver


def evidence_from_data(rows: list[dict]) -> dict[str, Evidence]:
    """Build an item-id -> Evidence map from a list of dicts."""
    out: dict[str, Evidence] = {}
    for row in rows:
        item_id = row["item_id"]
        waiver = None
        raw_waiver = row.get("waiver")
        if raw_waiver:
            waiver = Waiver(
                reason=raw_waiver.get("reason", ""),
                approver=raw_waiver.get("approver", ""),
                date=raw_waiver.get("date", ""),
                expiry=raw_waiver.get("expiry", ""),
            )
        out[item_id] = Evidence(
            item_id=item_id,
            status=Status(row.get("status", Status.NOT_RUN.value)),
            evidence=row.get("evidence", ""),
            verification_date=row.get("verification_date", ""),
            verifier=row.get("verifier", ""),
            notes=row.get("notes", ""),
            waiver=waiver,
        )
    return out


def load_evidence(path: str | Path) -> dict[str, Evidence]:
    """Load the evidence registry JSON file into an item-id -> Evidence map."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items", [])
    return evidence_from_data(data)


def default_evidence(checklist) -> dict[str, Evidence]:
    """The conservative baseline: every item NOT_RUN with no evidence."""
    return {
        item.id: Evidence(item_id=item.id, status=Status.NOT_RUN)
        for item in checklist
    }
