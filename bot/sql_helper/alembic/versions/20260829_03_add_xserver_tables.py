"""add xserver tables (测试服旁路扩展，与原有表隔离)

Revision ID: 20260829_03
Revises: 20260315_02
Create Date: 2026-08-29 12:00:00
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260829_03"
down_revision = "20260315_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS `xserver_account` (
          `server_id` VARCHAR(50) NOT NULL,
          `tg` BIGINT NOT NULL,
          `embyid` VARCHAR(255) NULL,
          `name` VARCHAR(255) NULL,
          `pwd` VARCHAR(255) NULL,
          `pwd2` VARCHAR(255) NULL,
          `lv` VARCHAR(1) NULL,
          `cr` DATETIME NULL,
          `ex` DATETIME NULL,
          `us` INT NULL,
          PRIMARY KEY (`server_id`, `tg`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS `xserver_quota` (
          `server_id` VARCHAR(50) NOT NULL,
          `total` INT NULL,
          `used` INT NULL,
          `update_time` DATETIME NULL,
          PRIMARY KEY (`server_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS `xserver_code` (
          `code` VARCHAR(50) NOT NULL,
          `server_id` VARCHAR(50) NULL,
          `tg` BIGINT NULL,
          `us` INT NULL,
          `kind` VARCHAR(10) NULL,
          `used` BIGINT NULL,
          `usedtime` DATETIME NULL,
          PRIMARY KEY (`code`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `xserver_code`;")
    op.execute("DROP TABLE IF EXISTS `xserver_quota`;")
    op.execute("DROP TABLE IF EXISTS `xserver_account`;")
