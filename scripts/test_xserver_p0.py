#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0 验证：xserver 配置模型 + 数据层原子性 + 服务层构造与建号流程。
运行：python scripts/test_xserver_p0.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _xserver_boot import boot, fresh_db  # noqa: E402

NS = boot()
sx = NS["sx"]
xs = NS["xs"]
Xserver = NS["Xserver"]
Config = NS["Config"]


# ---------------- schemas ----------------
class XserverSchemaTests(unittest.TestCase):
    def test_defaults_and_optional_field(self):
        cfg_dict = dict(
            bot_name="b", bot_token="t", owner_api=1, owner_hash="h", owner=1,
            group=[-1], main_group="m", chanel="c", bot_photo="", money="币",
            emby_api="k", emby_url="u", emby_line="l",
            db_host="h", db_user="u", db_pwd="p", db_name="d",
            open={"stat": False, "all_user": 10, "checkin": False, "exchange": True,
                  "whitelist": True, "invite": False, "leave_ban": True},
            ranks={"logo": "T"}, schedall={},
        )
        c = Config(**cfg_dict)
        self.assertEqual(c.xservers, [])
        c2 = Config(**cfg_dict, xservers=[{
            "id": "test", "url": "http://t:8096", "api": "k2", "all_user": 50,
            "channels": {"points_cost": 80},
        }])
        x = c2.xservers[0]
        self.assertEqual(x.expire_days, 15)
        self.assertEqual(x.name_prefix, "t_")
        self.assertTrue(x.channels.main_user)
        self.assertFalse(x.channels.whitelist)
        self.assertEqual(x.channels.points_cost, 80)


# ---------------- 数据层 ----------------
class XserverDataTests(unittest.TestCase):
    def setUp(self):
        fresh_db(NS)

    def test_quota_seed_is_idempotent(self):
        self.assertTrue(sx.xquota_ensure("test", total_seed=2))
        sx.xquota_ensure("test", total_seed=99)
        self.assertEqual(sx.xquota_get("test").total, 2)

    def test_quota_atomic_take_and_release(self):
        sx.xquota_ensure("test", 2)
        self.assertTrue(sx.xquota_take("test"))
        self.assertTrue(sx.xquota_take("test"))
        self.assertFalse(sx.xquota_take("test"))  # 超卖必须失败
        self.assertEqual(sx.xquota_get("test").used, 2)
        self.assertTrue(sx.xquota_give("test"))
        self.assertEqual(sx.xquota_get("test").used, 1)
        self.assertTrue(sx.xquota_take("test"))
        self.assertEqual(sx.xquota_get("test").used, 2)

    def test_quota_give_floor(self):
        sx.xquota_ensure("test", 1)
        sx.xquota_give("test")
        self.assertEqual(sx.xquota_get("test").used, 0)

    def test_account_composite_pk_isolation(self):
        ex = datetime(2099, 1, 1)
        self.assertTrue(sx.xacc_add("test", 111, "EID", "t_alice", "pwd", "1234", ex))
        a = sx.xacc_get("test", 111)
        self.assertIsNotNone(a)
        self.assertEqual(a.lv, "b")
        # 同一 tg 不同 server 不冲突
        self.assertTrue(sx.xacc_add("test2", 111, "EID2", "t_alice", "p", "1", ex))
        self.assertTrue(sx.xacc_update("test", 111, lv="c"))
        self.assertEqual(sx.xacc_get("test", 111).lv, "c")
        self.assertIsNotNone(sx.xacc_get_any("test", "t_alice"))
        self.assertIsNotNone(sx.xacc_get_any("test", "EID"))
        self.assertTrue(sx.xacc_delete("test", 111))
        self.assertIsNone(sx.xacc_get("test", 111))
        self.assertIsNotNone(sx.xacc_get("test2", 111))
        self.assertFalse(sx.xacc_delete("test", 111))  # 已删

    def test_quota_take_concurrent_no_oversell(self):
        """多线程并发占坑，成功数不得超过 total（sqlite 文件库以支持跨线程）"""
        import tempfile
        from sqlalchemy import create_engine as ce
        from sqlalchemy.pool import NullPool
        from sqlalchemy.orm import sessionmaker as smk
        from concurrent.futures import ThreadPoolExecutor
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        eng = None
        try:
            eng = ce(f"sqlite:///{path}", poolclass=NullPool, connect_args={"timeout": 30})
            sx.XserverAccount.__table__.create(eng)
            sx.XserverQuota.__table__.create(eng)
            sx.Session = smk(bind=eng)
            sx.xquota_ensure("c", 5)
            with ThreadPoolExecutor(max_workers=10) as pool:
                results = list(pool.map(lambda _: sx.xquota_take("c"), range(20)))
            self.assertEqual(sum(1 for r in results if r), 5)
            self.assertEqual(sx.xquota_get("c").used, 5)
        finally:
            if eng is not None:
                eng.dispose()
            fresh_db(NS)


# ---------------- 服务层 ----------------
class FakeResult:
    def __init__(self, success, data=None, error=None):
        self.success = success
        self.data = data
        self.error = error


class XserverServiceTests(unittest.TestCase):
    def setUp(self):
        fresh_db(NS)
        self.xc = Xserver(id="test", name="测试服", url="http://1.2.3.4:8096", api="K",
                          line="http://test.example.com:8096", all_user=3, expire_days=15,
                          block_libs=["nsfw"])
        NS["cfg"].xservers = [self.xc]

    def test_get_service_registry_and_seed(self):
        svc1 = xs.get_service("test")
        self.assertIsNotNone(svc1)
        svc2 = xs.get_service("test")
        self.assertIs(svc1, svc2)  # registry 复用
        q = sx.xquota_get("test")
        self.assertEqual(q.total, 3)  # ensure_ready 已种子

    def test_hot_update_rebuilds(self):
        svc1 = xs.get_service("test")
        NS["cfg"].xservers = [self.xc.model_copy(update={"url": "http://5.6.7.8:8096"})]
        svc2 = xs.get_service("test")
        self.assertIsNot(svc1, svc2)
        self.assertEqual(svc2.url, "http://5.6.7.8:8096")

    def test_disabled_or_unknown_returns_none(self):
        NS["cfg"].xservers = [self.xc.model_copy(update={"enable": False})]
        self.assertIsNone(xs.get_service("test"))
        self.assertIsNone(xs.get_service("nope"))

    def _fake_requests(self, svc, calls):
        async def fake_request(method, endpoint, **kw):
            calls.append((method, endpoint, kw.get("json")))
            if endpoint == "/emby/Users/New":
                return FakeResult(True, {"Id": "UID1"})
            if "/emby/Library/VirtualFolders" in endpoint:
                return FakeResult(True, [{"Guid": "G-nsfw", "Name": "nsfw"},
                                         {"Guid": "G-movie", "Name": "电影"}])
            if endpoint.startswith("/emby/Users/UID1") and method == "GET":
                return FakeResult(True, {"Policy": {"EnableAllFolders": True,
                                                    "EnabledFolders": ["G-nsfw", "G-movie"],
                                                    "BlockedMediaFolders": []}})
            return FakeResult(True, {})
        svc._request = fake_request

    def test_emby_create_x_flow(self):
        import asyncio
        svc = xs.get_service("test")
        calls = []
        self._fake_requests(svc, calls)
        res = asyncio.run(svc.emby_create_x("t_alice", 15, password="main_pwd"))
        self.assertEqual(res[0], "UID1")
        self.assertEqual(res[1], "main_pwd")  # 密码原样复制
        self.assertTrue(res[2] > datetime.now() + timedelta(days=14))
        pwd_call = next(c for c in calls if c[1].endswith("/Password"))
        self.assertEqual(pwd_call[2]["NewPw"], "main_pwd")
        first_pol = next(c for c in calls if c[1].endswith("/Policy"))[2]
        self.assertEqual(first_pol["SimultaneousStreamLimit"], 2)
        self.assertIn("nsfw", first_pol["BlockedMediaFolders"])   # 独立 block 配置
        self.assertIn("播放列表", first_pol["BlockedMediaFolders"])

    def test_emby_create_x_rollback_on_password_fail(self):
        import asyncio
        svc = xs.get_service("test")
        calls = []

        async def fake_request(method, endpoint, **kw):
            calls.append((method, endpoint, kw.get("json")))
            if endpoint == "/emby/Users/New":
                return FakeResult(True, {"Id": "UID1"})
            if endpoint.endswith("/Password"):
                return FakeResult(False, error="boom")
            return FakeResult(True, {})

        svc._request = fake_request
        res = asyncio.run(svc.emby_create_x("t_bob", 15))
        self.assertFalse(res)
        self.assertIn(("DELETE", "/emby/Users/UID1", None), calls)  # 半成品已远端删除


if __name__ == "__main__":
    unittest.main(verbosity=2)
