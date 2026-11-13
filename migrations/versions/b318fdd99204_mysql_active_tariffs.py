"""Portable uniqueness of active tariffs using a generated column."""

import sqlalchemy as sa
from alembic import op

revision = "b318fdd99204"
down_revision = "ac7b28de1a86"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("uq_tariffs_active_lessons", table_name="tariffs")
    op.add_column(
        "tariffs",
        sa.Column(
            "active_lessons",
            sa.Integer(),
            sa.Computed("CASE WHEN archived = 0 THEN lessons ELSE NULL END", persisted=False),
            nullable=True,
        ),
    )
    op.create_index("uq_tariffs_active_lessons", "tariffs", ["active_lessons"], unique=True)


def downgrade():
    op.drop_index("uq_tariffs_active_lessons", table_name="tariffs")
    op.drop_column("tariffs", "active_lessons")
    op.create_index(
        "uq_tariffs_active_lessons",
        "tariffs",
        ["lessons"],
        unique=True,
        postgresql_where=sa.text("NOT archived"),
        sqlite_where=sa.text("archived = 0"),
    )
