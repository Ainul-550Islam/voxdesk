"""enum consistency: callstatus + speaker

Reconciles the Python enums with the PostgreSQL types.

WHY THIS EXISTS
---------------
Two separate drifts had accumulated:

  callstatus
    - alembic 0001_baseline created: RINGING, IN_PROGRESS, COMPLETED, FAILED,
      NO_ANSWER
    - the Python enum declared:      RINGING, IN_PROGRESS, COMPLETED, FAILED,
      TRANSFERRED
    Each side was missing one member the other had. Databases bootstrapped by
    the `Base.metadata.create_all` call in app/main.py therefore have a
    *different* type from databases bootstrapped by Alembic.

  speaker
    - alembic 0001_baseline created: USER, ASSISTANT, SYSTEM
    - the Python enum declared:      CALLER, AGENT, SYSTEM
    SQLAlchemy persists the member *name*, so an Alembic-managed database
    rejected every transcript row the voice pipeline tried to write.

This migration converges both types on the Python definitions and is safe to
run against either drift state, or against an already-correct database.

DATA SAFETY
-----------
  * callstatus: values are only ADDED. No row is touched.
  * speaker:    the type is recreated and existing rows are remapped
                CALLER -> USER and AGENT -> ASSISTANT. No row is deleted.

Revision ID: 0002_enum_consistency
Revises: 0001_baseline
"""
from alembic import op

revision = "0002_enum_consistency"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


# The single source of truth, mirroring app/db/models.py.
CALL_STATUS_VALUES = (
    "RINGING", "IN_PROGRESS", "COMPLETED", "FAILED", "NO_ANSWER", "TRANSFERRED",
)
SPEAKER_VALUES = ("USER", "ASSISTANT", "SYSTEM")

# Legacy names that may exist in a database created by metadata.create_all.
SPEAKER_LEGACY_MAP = {"CALLER": "USER", "AGENT": "ASSISTANT"}


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite and friends render Enum() as VARCHAR + CHECK; the constraint is
        # rebuilt from the model on create_all, so there is nothing to migrate.
        return

    # ---------------------------------------------------------- callstatus ---
    # ALTER TYPE ... ADD VALUE cannot be used in the same transaction that adds
    # it, so it runs in its own autocommit block. IF NOT EXISTS makes this
    # idempotent across both drift states and on re-runs.
    with op.get_context().autocommit_block():
        for value in CALL_STATUS_VALUES:
            op.execute(
                f"ALTER TYPE callstatus ADD VALUE IF NOT EXISTS '{value}'"
            )

    # ------------------------------------------------------------- speaker ---
    # Removing a value from a PostgreSQL enum is not possible in place, so the
    # type is recreated and the column is cast across with an explicit mapping.
    case_arms = " ".join(
        f"WHEN '{old}' THEN '{new}'" for old, new in SPEAKER_LEGACY_MAP.items()
    )
    new_values = ", ".join(f"'{v}'" for v in SPEAKER_VALUES)

    op.execute("ALTER TYPE speaker RENAME TO speaker_legacy")
    op.execute(f"CREATE TYPE speaker AS ENUM ({new_values})")
    op.execute(
        "ALTER TABLE turns "
        "ALTER COLUMN speaker TYPE speaker "
        "USING ("
        "  CASE speaker::text "
        f"    {case_arms} "
        "    ELSE speaker::text "
        "  END"
        ")::speaker"
    )
    op.execute("DROP TYPE speaker_legacy")


def downgrade() -> None:
    """
    Reverts to the exact types 0001_baseline created.

    `speaker` is already USER/ASSISTANT/SYSTEM in the baseline, so it is left
    alone -- reintroducing CALLER/AGENT would recreate the bug.

    `callstatus` loses TRANSFERRED, which the baseline never had. Any call
    still holding that status is remapped to COMPLETED, because a transferred
    call did in fact complete. This is the only lossy step, and it only
    affects a status column, never a transcript.
    """
    if not _is_postgres():
        return

    baseline_values = tuple(v for v in CALL_STATUS_VALUES if v != "TRANSFERRED")
    new_values = ", ".join(f"'{v}'" for v in baseline_values)

    op.execute("ALTER TYPE callstatus RENAME TO callstatus_new")
    op.execute(f"CREATE TYPE callstatus AS ENUM ({new_values})")
    op.execute("ALTER TABLE calls ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE calls "
        "ALTER COLUMN status TYPE callstatus "
        "USING ("
        "  CASE status::text "
        "    WHEN 'TRANSFERRED' THEN 'COMPLETED' "
        "    ELSE status::text "
        "  END"
        ")::callstatus"
    )
    op.execute("ALTER TABLE calls ALTER COLUMN status SET DEFAULT 'RINGING'")
    op.execute("DROP TYPE callstatus_new")

