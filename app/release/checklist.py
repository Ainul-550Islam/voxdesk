"""Canonical launch checklist loader (Step 11).

The single source of truth for every launch requirement is
``scripts/release/checklist.json``. This module loads and validates it and
converts the JSON shape into ``ChecklistItem`` values. Keeping the data in
JSON (rather than Python) means an operator can read and, with review, amend
the checklist without touching code — but the gate still validates ids are
unique and severities/classifications are legal, so a typo fails loudly
instead of silently changing the gate.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.release.models import ChecklistItem, Classification, Severity


def checklist_from_data(rows: list[dict]) -> list[ChecklistItem]:
    """Build items from a list of dicts (the JSON ``items`` shape)."""
    items: list[ChecklistItem] = []
    seen: set[str] = set()
    for row in rows:
        item = ChecklistItem(
            id=row["id"],
            category=row["category"],
            requirement=row["requirement"],
            severity=Severity(row["severity"]),
            classification=Classification(row["classification"]),
            evidence_hint=row.get("evidence_hint", ""),
            notes=row.get("notes", ""),
        )
        if item.id in seen:
            raise ValueError(f"duplicate checklist item id {item.id!r}")
        seen.add(item.id)
        items.append(item)
    return items


def load_checklist(path: str | Path) -> list[ChecklistItem]:
    """Load the canonical checklist JSON file into items."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items", [])
    return checklist_from_data(data)
