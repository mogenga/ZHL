"""PB L2/L3: reinspection scope columns + capa_verified

Revision ID: 002_reinspection_scope
Revises: 001_init
Create Date: 2026-10-11

Notes
-----
The baseline revision 001_init is a marker for the Phase 0 schema, whose DDL
lives in ``migrations/001_init.sql`` and was applied outside alembic via
``psql $ZHL_DB -f 001_init.sql`` (see ``alembic/versions/001_init.py``).
Before running this revision, stamp the baseline so alembic knows 001 is
already applied::

    alembic stamp 001_init
    alembic upgrade head

The downgrade path drops the three added columns (no data is lost because
they all have sensible defaults and were NOT NULL).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
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
    # The CHECK rides inline inside ADD COLUMN (as in the psql file), so
    # PostgreSQL auto-names it reinspections_scope_type_check — running the
    # upgrade via alembic or psql leaves the table in the identical shape.
    op.execute(
        "ALTER TABLE reinspections ADD COLUMN scope_type "
        "VARCHAR(40) NOT NULL DEFAULT 'minimal' "
        "CHECK (scope_type IN ('minimal', 'expanded', 'full_lot'))"
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
    bind = op.get_bind()
    # The inline CHECK is auto-named by PG (reinspections_scope_type_check);
    # accept a named ck_reinspections_scope_type too, so databases upgraded
    # either way (alembic vs psql -f) can both roll back. Dropping the
    # column also removes the CHECK, but the explicit drop keeps the
    # sequence clear for older PG versions.
    inspector = sa_inspect(bind)
    constraint_names = {c["name"] for c in inspector.get_unique_constraints("reinspections")}
    constraint_names |= {c["name"] for c in inspector.get_check_constraints("reinspections")}
    for name in ("reinspections_scope_type_check", "ck_reinspections_scope_type"):
        if name in constraint_names:
            op.drop_constraint(name, "reinspections")
    op.drop_column("quality_incidents", "capa_verified")
    op.drop_column("reinspections", "scope_sns")
    op.drop_column("reinspections", "scope_type")
