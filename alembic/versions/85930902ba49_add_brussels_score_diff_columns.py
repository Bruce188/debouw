"""add brussels score diff columns

Mirrors three new PermitProject fields added in Phase 6 (feat/brussels-score-differentiation):
  - error_weight: float | None  (from tabledatahistory errorWeight key)
  - floor_area_m2: float | None (sum of authorized floor area by typology)
  - case_language: str | None   (language of the case: "fr" or "nl")

Revision ID: 85930902ba49
Revises: 4f9d56aedd52
Create Date: 2026-04-30 16:15:53.555645

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '85930902ba49'
down_revision: Union[str, Sequence[str], None] = '4f9d56aedd52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add three nullable columns to permit_projects."""
    op.add_column('permit_projects', sa.Column('error_weight', sa.Float(), nullable=True))
    op.add_column('permit_projects', sa.Column('floor_area_m2', sa.Float(), nullable=True))
    op.add_column('permit_projects', sa.Column('case_language', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema: drop the three Brussels score-diff columns."""
    op.drop_column('permit_projects', 'case_language')
    op.drop_column('permit_projects', 'floor_area_m2')
    op.drop_column('permit_projects', 'error_weight')
