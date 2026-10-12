"""PB L2/L3: reinspection scope columns + capa_verified

Revision ID: 002_reinspection_scope
Revises: None
Create Date: 2026-10-11

Notes
-----
This is the first alembic revision. Phase 0 initialised ``alembic/`` but did
not generate a revision (DDL ran via ``psql -f 001_init.sql``). Before running
this revision, stamp the head of the Phase 0 schema so alembic knows 001 is
already applied::

    alembic stamp 001_init
    alembic upgrade head

The downgrade path drops the three added columns (no data is lost because
they all have sensible defaults and were NOT NULL).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision = "002_reinspection_scope"
down_revision = "001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- PB L2: reinspections.scope_type / scope_sns ----
    # VARCHAR + CHECK (models.py uses native_enum=False for cross-DB parity);
    # matches the DDL in migrations/002_add_reinspection_scope.sql.
    op.add_column(
        "reinspections",
        sa.Column(
            "scope_type",
            sa.String(40),
            nullable=False,
            server_default="minimal",
        ),
    )
    op.add_column(
        "reinspections",
        sa.Column(
            "scope_sns",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # CHECK constraint (named so downgrade can drop it explicitly).
    op.create_check_constraint(
        "ck_reinspections_scope_type",
        "reinspections",
        "scope_type IN ('minimal', 'expanded', 'full_lot')",
    )

    # ---- PB L3: quality_incidents.capa_verified ----
    op.add_column(
        "quality_incidents",
        sa.Column(
            "capa_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )


def downgrade() -> None:
    # Drop CHECK first (column drop would fail otherwise on PG).
    op.drop_constraint("ck_reinspections_scope_type", "reinspections")
    op.drop_column("quality_incidents", "capa_verified")
    op.drop_column("reinspections", "scope_sns")
    op.drop_column("reinspections", "scope_type")
