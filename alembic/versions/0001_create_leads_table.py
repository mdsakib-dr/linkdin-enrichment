"""create leads table

Revision ID: 0001
Revises:
Create Date: 2026-07-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("linkedin_url", sa.String(length=512), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("job_title", sa.String(length=512), nullable=True),
        sa.Column("company_name", sa.String(length=512), nullable=True),
        sa.Column("location", sa.String(length=512), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("mobile_number", sa.String(length=64), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("search_query", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("linkedin_url", name="uq_leads_linkedin_url"),
    )
    op.create_index(
        "ix_leads_linkedin_url", "leads", ["linkedin_url"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_leads_linkedin_url", table_name="leads")
    op.drop_table("leads")
