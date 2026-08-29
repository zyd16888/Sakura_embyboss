#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (ROOT / "bot" / "sql_helper" / "alembic" / "versions" /
             "20260830_04_add_xserver_history.py")


class OpRecorder:
    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)


op = OpRecorder()
alembic_stub = types.ModuleType("alembic")
alembic_stub.op = op
sys.modules["alembic"] = alembic_stub

spec = importlib.util.spec_from_file_location("xserver_history_migration", MIGRATION)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class XserverHistoryMigrationTests(unittest.TestCase):
    def setUp(self):
        op.statements.clear()

    def test_revision_runs_after_existing_xserver_revision(self):
        self.assertEqual(migration.revision, "20260830_04")
        self.assertEqual(migration.down_revision, "20260829_03")

    def test_upgrade_creates_missing_history_table_idempotently(self):
        migration.upgrade()

        sql = "\n".join(op.statements)
        self.assertIn("CREATE TABLE IF NOT EXISTS `xserver_history`", sql)
        self.assertIn("PRIMARY KEY (`server_id`, `tg`)", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
