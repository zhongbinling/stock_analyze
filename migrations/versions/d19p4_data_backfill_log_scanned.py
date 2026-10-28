"""D1-19 P4: data_backfill_log 加 scanned_n 列(扫描交易日数, 与写入日分离)

Revision ID: d19p4bfsc
Revises: b8e2f7a91c34
Create Date: 2026-10-28 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd19p4bfsc'
down_revision = 'b8e2f7a91c34'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('data_backfill_log', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scanned_n', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('data_backfill_log', schema=None) as batch_op:
        batch_op.drop_column('scanned_n')
