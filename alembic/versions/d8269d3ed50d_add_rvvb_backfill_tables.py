"""add_rvvb_backfill_tables

Revision ID: d8269d3ed50d
Revises: 62d866f60c16
Create Date: 2026-04-26 17:17:40.326997

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8269d3ed50d'
down_revision: Union[str, Sequence[str], None] = '62d866f60c16'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "arrest_extraction_cache",
        sa.Column("arrest_id", sa.String(), nullable=False),
        sa.Column("extractor_version", sa.String(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("arrest_id", "extractor_version"),
    )
    op.create_table(
        "rvvb_backfill_state",
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("last_page_processed", sa.Integer(), nullable=True),
        sa.Column("last_arrest_id_processed", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("source"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("rvvb_backfill_state")
    op.drop_table("arrest_extraction_cache")
