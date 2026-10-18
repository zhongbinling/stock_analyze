"""B1: backup_runs 备份执行记录表

Revision ID: a7f3c2d81e94
Revises: 2c59761f16a1
Create Date: 2026-10-18 06:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7f3c2d81e94'
down_revision = '2c59761f16a1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'backup_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('btype', sa.String(length=12), nullable=False),
        sa.Column('trigger', sa.String(length=8), nullable=True),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('files_n', sa.Integer(), nullable=True),
        sa.Column('total_mb', sa.Float(), nullable=True),
        sa.Column('duration_s', sa.Float(), nullable=True),
        sa.Column('error', sa.String(length=255), nullable=True),
        sa.Column('files_json', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_backup_runs_started_at', 'backup_runs', ['started_at'])


def downgrade():
    op.drop_index('ix_backup_runs_started_at', table_name='backup_runs')
    op.drop_table('backup_runs')
