#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BadRequest(Exception):
    def __init__(self, error_id):
        super().__init__(error_id)
        self.ID = error_id


class Status:
    ADMINISTRATOR = "administrator"
    MEMBER = "member"
    OWNER = "owner"
    RESTRICTED = "restricted"


class Logger:
    def error(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


bot_stub = types.ModuleType("bot")
bot_stub.admins = [9]
bot_stub.owner = 1
bot_stub.group = [-1001, -1002]
bot_stub.LOGGER = Logger()
sys.modules["bot"] = bot_stub

pyrogram_stub = types.ModuleType("pyrogram")
filters_stub = types.ModuleType("pyrogram.filters")
filters_stub.create = lambda fn: fn
errors_stub = types.ModuleType("pyrogram.errors")
errors_stub.BadRequest = BadRequest
enums_stub = types.ModuleType("pyrogram.enums")
enums_stub.ChatMemberStatus = Status
pyrogram_stub.filters = filters_stub
sys.modules["pyrogram"] = pyrogram_stub
sys.modules["pyrogram.filters"] = filters_stub
sys.modules["pyrogram.errors"] = errors_stub
sys.modules["pyrogram.enums"] = enums_stub

spec = importlib.util.spec_from_file_location(
    "filters_under_test", ROOT / "bot" / "func_helper" / "filters.py"
)
filters_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(filters_module)


class FakeUpdate:
    def __init__(self, uid):
        self.from_user = types.SimpleNamespace(id=uid)
        self.sender_chat = None
        self.alerts = []

    async def answer(self, text, show_alert=False):
        self.alerts.append((text, show_alert))


class FakeClient:
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        result = self.results[chat_id]
        if isinstance(result, Exception):
            raise result
        return result


def run(coro):
    return asyncio.run(coro)


class GroupFilterTests(unittest.TestCase):
    def setUp(self):
        filters_module.group = [-1001, -1002]
        filters_module.admins = [9]
        filters_module.owner = 1

    def test_restricted_group_member_is_allowed(self):
        member = types.SimpleNamespace(status=Status.RESTRICTED, is_member=True)
        client = FakeClient({-1001: member, -1002: BadRequest("USER_NOT_PARTICIPANT")})

        allowed = run(filters_module.user_in_group_on_filter(None, client, FakeUpdate(100)))

        self.assertTrue(allowed)

    def test_membership_search_continues_across_all_groups(self):
        member = types.SimpleNamespace(status=Status.MEMBER, is_member=True)
        client = FakeClient({
            -1001: BadRequest("USER_NOT_PARTICIPANT"),
            -1002: member,
        })

        allowed = run(filters_module.user_in_group_on_filter(None, client, FakeUpdate(101)))

        self.assertTrue(allowed)
        self.assertEqual(client.calls, [(-1001, 101), (-1002, 101)])

    def test_lookup_error_returns_visible_callback_alert(self):
        client = FakeClient({
            -1001: RuntimeError("transport closed"),
            -1002: BadRequest("USER_NOT_PARTICIPANT"),
        })
        update = FakeUpdate(102)

        allowed = run(filters_module.user_in_group_on_filter(None, client, update))

        self.assertFalse(allowed)
        self.assertIn("资格检查暂时失败", update.alerts[0][0])
        self.assertTrue(update.alerts[0][1])

    def test_non_member_gets_join_group_alert(self):
        client = FakeClient({
            -1001: BadRequest("USER_NOT_PARTICIPANT"),
            -1002: BadRequest("USER_NOT_PARTICIPANT"),
        })
        update = FakeUpdate(103)

        allowed = run(filters_module.user_in_group_on_filter(None, client, update))

        self.assertFalse(allowed)
        self.assertIn("请先加入授权群组", update.alerts[0][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
