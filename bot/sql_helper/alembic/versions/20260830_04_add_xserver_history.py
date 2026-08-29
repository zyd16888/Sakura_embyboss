"""create xserver history table for databases already stamped at revision 03

Revision ID: 20260830_04
Revises: 20260829_03
Create Date: 2026-08-30 05:10:00
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260830_04"
down_revision = "20260829_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS `xserver_history` (
          `server_id` VARCHAR(50) NOT NULL,
          `tg` BIGINT NOT NULL,
          `first_cr` DATETIME NULL,
          `last_cr` DATETIME NULL,
          `open_count` INT NULL,
          PRIMARY KEY (`server_id`, `tg`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `xserver_history`;")
