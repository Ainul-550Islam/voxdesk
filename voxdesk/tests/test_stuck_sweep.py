"""Step 7 — the read-only stuck-side-effect snapshot."""
from __future__ import annotations

import pytest

from app.core import observability


@pytest.mark.asyncio
async def test_stuck_counts_on_empty_database(db):
    """An empty database reports zero stuck rows for both kinds — the sweep
    is a pure count, never a mutation."""
    counts = await observability.stuck_side_effect_counts(db)
    assert counts == {"crm_sync": 0, "knowledge_document": 0}
