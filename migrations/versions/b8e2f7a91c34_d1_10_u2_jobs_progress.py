"""D1-10 U2: jobs 表加 progress 列

Revision ID: b8e2f7a91c34
Revises: c9d21e4f7a30
Create Date: 2026-10-24 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b8e2f7a91c34'
down_revision = 'c9d21e4f7a30'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('progress', sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('progress')
