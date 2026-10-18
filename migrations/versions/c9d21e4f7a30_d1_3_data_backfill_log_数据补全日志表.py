"""D1-3: data_backfill_log 数据补全日志表

Revision ID: c9d21e4f7a30
Revises: a7f3c2d81e94
Create Date: 2026-10-18 08:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9d21e4f7a30'
down_revision = 'a7f3c2d81e94'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'data_backfill_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('dataset', sa.String(length=16), nullable=False),
        sa.Column('label', sa.String(length=48), nullable=True),
        sa.Column('date_from', sa.String(length=10), nullable=True),
        sa.Column('date_to', sa.String(length=10), nullable=True),
        sa.Column('rows_written', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('trigger', sa.String(length=8), nullable=True),
        sa.Column('days_json', sa.Text(), nullable=True),
        sa.Column('duration_s', sa.Float(), nullable=True),
        sa.Column('error', sa.String(length=255), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_dbl_dataset', 'data_backfill_log', ['dataset'])
    op.create_index('ix_dbl_started_at', 'data_backfill_log', ['started_at'])


def downgrade():
    op.drop_index('ix_dbl_started_at', table_name='data_backfill_log')
    op.drop_index('ix_dbl_dataset', table_name='data_backfill_log')
    op.drop_table('data_backfill_log')
