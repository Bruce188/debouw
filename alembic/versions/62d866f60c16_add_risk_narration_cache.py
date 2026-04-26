"""add_risk_narration_cache

Revision ID: 62d866f60c16
Revises: e9761d793952
Create Date: 2026-04-26 16:14:26.660232

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '62d866f60c16'
down_revision: Union[str, Sequence[str], None] = 'e9761d793952'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "risk_narration_cache",
        sa.Column("project_external_id", sa.String(), nullable=False),
        sa.Column("engine_version", sa.String(), nullable=False),
        sa.Column("rationales_json", sa.JSON(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("project_external_id", "engine_version"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("risk_narration_cache")
