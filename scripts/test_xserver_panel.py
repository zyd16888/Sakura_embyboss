#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P1/P2 验证：xserver 资格矩阵、开号流程（一键/积分/资格/收编/注销）、
注册码兑换、到期删除任务。
运行：python scripts/test_xserver_panel.py
"""
import asyncio
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _xserver_boot import boot, fresh_db  # noqa: E402

NS = boot()
sx = NS["sx"]
se = NS["se"]
xs = NS["xs"]
Xserver = NS["Xserver"]
captured = NS["captured"]

from bot.modules.panel import xserver_panel as xp  # noqa: E402
from bot.modules.panel import xserver_admin as xa  # noqa: E402
from bot.modules.commands import xserver_code as xcode  # noqa: E402
from bot.scheduler import xserver_ex  # noqa: E402


# ---------------- 测试替身 ----------------

class FakeUser:
    def __init__(self, uid, name="u"):
        self.id = uid
        self.first_name = name


class FakeCall:
    def __init__(self, uid, data="x"):
        self.from_user = FakeUser(uid)
        self.data = data
        self.message = None


class FakeMsg:
    def __init__(self, uid, text="", name="u"):
        self.from_user = FakeUser(uid, name)
        self.text = text

    async def delete(self):
        return True

    async def reply(self, text, **kw):
        captured.append(("reply", text))
        return FakeMsg(0, text)


class FakeService:
    def __init__(self, create_ok=True):
        self.create_ok = create_ok
        self.deleted = []
        self.disabled = []
        self.reset = []
        self.policy = []
        self.existing = {}
        self.calls = []

    async def get_emby_user_by_name(self, name):
        if name in self.existing:
            return True, {"Id": self.existing[name], "Name": name}
        return False, {"error": "🤕用户不存在"}

    async def emby_create_x(self, name, days, password=None):
        self.calls.append(("create", name, days, password))
        if not self.create_ok:
            return False
        return f"ID-{name}", (password or "genpwd"), datetime.now() + timedelta(days=days)

    async def emby_reset(self, eid, pwd):
        self.reset.append((eid, pwd))
        return True

    async def emby_change_policy(self, eid, admin=False, disable=False):
        self.policy.append((eid, disable))
        return True

    async def x_delete(self, eid):
        self.deleted.append(eid)
        return True

    async def user(self, eid):
        return False, {"error": "资源不存在"}


def setup_server(total=10, open_total=None, **over):
    # 默认 allow_reopen=True 保持历史回滚类用例语义；复开限制单独测
    over.setdefault('allow_reopen', True)
    fresh_db(NS)
    xc = Xserver(id="test", name="测试服", url="http://t:8096", api="K",
                 line="http://t.line", all_user=total,
                 all_user_open=total if open_total is None else open_total, **over)
    NS["cfg"].xservers = [xc]
    sx.xquota_ensure("test", total, xc.all_user_open)
    return xc


def add_main(tg, name="alice", pwd="pw123", pwd2="9999", lv="b", iv=500, embyid="MID"):
    with se.Session() as s:
        s.add(se.Emby(tg=tg, embyid=embyid, name=name, pwd=pwd, pwd2=pwd2,
                      lv=lv, cr=datetime.now(), ex=datetime.now() + timedelta(days=30),
                      us=0, iv=iv))
        s.commit()


def patch_service(svc):
    """两个模块都是 `from ... import get_service`，需分别打补丁"""
    xp.get_service = lambda sid: svc
    xs.get_service = lambda sid: svc
    xserver_ex.get_service = lambda sid: svc


def run(coro):
    return asyncio.run(coro)


def open_account(call, sid, name, pwd, pwd2, copied=True):
    return run(xp._occupy_and_open(call, sid, name, pwd, pwd2, copied=copied))


# ---------------- 资格矩阵 ----------------

class EligibilityTests(unittest.TestCase):
    def test_matrix(self):
        xc = setup_server()
        main_ok = types.SimpleNamespace(embyid="E", lv="b", iv=100)
        main_wl = types.SimpleNamespace(embyid="E", lv="a", iv=100)
        main_none = types.SimpleNamespace(embyid=None, lv="d", iv=100)
        grant = types.SimpleNamespace(us=30, embyid=None)

        self.assertEqual(xp._eligibility(xc, main_ok, None), ('free', 0))
        self.assertEqual(xp._eligibility(xc, main_none, None), ('points', 100))
        self.assertEqual(xp._eligibility(xc, None, None), ('none', 0))
        self.assertEqual(xp._eligibility(xc, main_none, grant), ('grant', 0))

        xc.channels.main_user = False
        xc.channels.whitelist = True
        self.assertEqual(xp._eligibility(xc, main_wl, None), ('free', 0))
        self.assertEqual(xp._eligibility(xc, main_ok, None), ('points', 100))

        xc.channels.points = False
        self.assertEqual(xp._eligibility(xc, main_ok, None), ('none', 0))
        # 管理员直发资格不受通道开关影响
        self.assertEqual(xp._eligibility(xc, main_ok, grant), ('grant', 0))


# ---------------- 开号流程 ----------------

class OpenFlowTests(unittest.TestCase):
    def test_quick_copy_credentials(self):
        setup_server()
        add_main(111, name="alice", pwd="pw123", pwd2="9999")
        svc = FakeService()
        patch_service(svc)
        ok, out = open_account(FakeCall(111), "test", "t_alice", "pw123", "9999")
        self.assertTrue(ok, out)
        self.assertEqual(svc.calls[0], ("create", "t_alice", 15, "pw123"))
        a = sx.xacc_get("test", 111)
        self.assertEqual((a.name, a.pwd, a.pwd2, a.lv), ("t_alice", "pw123", "9999", "b"))
        self.assertEqual(sx.xquota_get("test").used, 1)
        self.assertIn("开号成功", out)

    def test_points_deduct_and_full_rollback(self):
        setup_server()
        add_main(222, name="bob", iv=250, embyid=None, lv='d')  # 无主服号 → 积分通道+open池
        svc = FakeService()
        patch_service(svc)
        ok, out = open_account(FakeCall(222), "test", "t_custom", None, "1234", copied=False)
        self.assertTrue(ok, out)
        self.assertEqual(se.sql_get_emby(222).iv, 150)   # 扣 100
        q = sx.xquota_get("test")
        self.assertEqual((q.used, q.used_open), (0, 1))  # 占的是 open 池
        self.assertEqual(sx.xacc_get("test", 222).pool, sx.POOL_OPEN)

        # 注销：退 open 池名额、不退积分
        run(xp.xs_del_confirm(None, FakeCall(222, "xs:dc:" + xp._enc("test"))))
        q = sx.xquota_get("test")
        self.assertEqual((q.used, q.used_open), (0, 0))
        self.assertEqual(se.sql_get_emby(222).iv, 150)

        # 再次开号但远端建号失败 → 积分/open池名额回滚
        patch_service(FakeService(create_ok=False))
        ok2, out2 = open_account(FakeCall(222), "test", "t_other", None, "1234", copied=False)
        self.assertFalse(ok2)
        self.assertEqual(se.sql_get_emby(222).iv, 150)
        q = sx.xquota_get("test")
        self.assertEqual((q.used, q.used_open), (0, 0))

    def test_pool_isolation_main_full_open_usable(self):
        """main 池满：主服用户被拒，但无主服号用户仍可占 open 池"""
        setup_server(total=1, open_total=2)
        add_main(1, name="a1")
        add_main(2, name="a2", iv=300)
        patch_service(FakeService())
        ok1, _ = open_account(FakeCall(1), "test", "t_a1", "p", "1")
        self.assertTrue(ok1)                                   # main 池 1/1
        ok2, out2 = open_account(FakeCall(2), "test", "t_a2", "p", "2")
        self.assertFalse(ok2)
        self.assertIn("主服用户名额已满", out2)
        # 把 2 改成无主服号状态 → 走 open 池成功
        se.sql_update_emby(se.Emby.tg == 2, embyid=None, lv='d')
        ok3, out3 = open_account(FakeCall(2), "test", "t_a2b", "p", "2", copied=False)
        self.assertTrue(ok3, out3)
        q = sx.xquota_get("test")
        self.assertEqual((q.used, q.used_open), (1, 1))

    def test_slot_full_and_name_occupied(self):
        setup_server(total=1)
        add_main(1, name="a1")
        add_main(2, name="a2")
        svc = FakeService()
        patch_service(svc)
        ok, _ = open_account(FakeCall(1), "test", "t_a1", "p", "1")
        self.assertTrue(ok)
        ok2, out2 = open_account(FakeCall(2), "test", "t_a2", "p", "2")
        self.assertFalse(ok2)
        self.assertIn("名额已满", out2)

        sx.xquota_set_total("test", 5)
        ok3, out3 = open_account(FakeCall(2), "test", "t_a1", "p", "2")
        self.assertFalse(ok3)
        self.assertIn("已被占用", out3)
        self.assertEqual(sx.xquota_get("test").used, 1)  # 拒绝路径不占坑

    def test_no_double_open(self):
        setup_server()
        add_main(111)
        patch_service(FakeService())
        ok, _ = open_account(FakeCall(111), "test", "t_alice", "p", "1")
        self.assertTrue(ok)
        ok2, out2 = open_account(FakeCall(111), "test", "t_again", "p", "1")
        self.assertFalse(ok2)
        self.assertIn("已有账号", out2)
        self.assertEqual(sx.xquota_get("test").used, 1)

    def test_recapture_remote_leftover(self):
        setup_server()
        add_main(111, name="alice", pwd="pw123", pwd2="9999")
        svc = FakeService()
        svc.existing["t_alice"] = "REMOTE-9"
        patch_service(svc)
        ok, out = open_account(FakeCall(111), "test", "t_alice", "pw123", "9999")
        self.assertTrue(ok, out)
        self.assertEqual(svc.reset, [("REMOTE-9", "pw123")])
        self.assertIn(("REMOTE-9", False), svc.policy)
        self.assertEqual(sx.xacc_get("test", 111).embyid, "REMOTE-9")
        self.assertEqual(svc.calls, [])  # 未走 create

    def test_quick_entry_checks_main_state(self):
        setup_server()
        add_main(111, name="alice", lv="c")  # 主服被封印
        patch_service(FakeService())
        call = FakeCall(111, "xs:q:" + xp._enc("test"))
        run(xp.xs_quick(None, call))
        self.assertIsNone(sx.xacc_get("test", 111))
        self.assertTrue(any("状态异常" in str(item) for item in captured))

    def test_delme_releases_quota(self):
        setup_server()
        add_main(111)
        svc = FakeService()
        patch_service(svc)
        open_account(FakeCall(111), "test", "t_alice", "pw", "9")
        self.assertEqual(sx.xquota_get("test").used, 1)
        run(xp.xs_del_confirm(None, FakeCall(111, "xs:dc:" + xp._enc("test"))))
        self.assertEqual(svc.deleted, ["ID-t_alice"])
        self.assertIsNone(sx.xacc_get("test", 111))
        self.assertEqual(sx.xquota_get("test").used, 0)


# ---------------- 注册码 ----------------

class CodeTests(unittest.TestCase):
    def test_is_xserver_code(self):
        self.assertTrue(xcode.is_xserver_code("T-test-XREG_abc"))
        self.assertTrue(xcode.is_xserver_code("T-test-XRNV_abc"))
        self.assertFalse(xcode.is_xserver_code("T-1-Register_xyz"))
        self.assertFalse(xcode.is_xserver_code(""))

    def test_redeem_unknown_code_intercepted(self):
        setup_server()
        add_main(333)
        handled = run(xcode.xs_redeem_code(FakeMsg(333), "T-test-XREG_nosuch"))
        self.assertTrue(handled)
        self.assertTrue(any("无效" in str(c) for c in captured))

    def test_reg_code_flow_grant_then_open(self):
        setup_server()
        xc = NS["cfg"].xservers[0]
        xc.channels.main_user = False   # 模拟纯资格用户
        xc.channels.points = False
        add_main(444, name="frank", embyid=None, lv="d")
        self.assertTrue(sx.xcode_add(["T-test-XREG_good"], "test", 1, 30, "reg"))
        handled = run(xcode.xs_redeem_code(FakeMsg(444), "T-test-XREG_good"))
        self.assertTrue(handled)
        a = sx.xacc_get("test", 444)
        self.assertIsNotNone(a)
        self.assertEqual(int(a.us), 30)
        self.assertIsNone(a.embyid)

        # 二次兑换同码 → 已被使用
        handled2 = run(xcode.xs_redeem_code(FakeMsg(999), "T-test-XREG_good"))
        self.assertTrue(handled2)
        self.assertTrue(any("已被使用" in str(c) for c in captured))

        # 资格开号：有效期=30 天，资格清零
        svc = FakeService()
        patch_service(svc)
        ok, out = open_account(FakeCall(444), "test", "t_f", None, "1", copied=False)
        self.assertTrue(ok, out)
        self.assertEqual(svc.calls[0][2], 30)
        a2 = sx.xacc_get("test", 444)
        self.assertEqual(int(a2.us), 0)
        self.assertIsNotNone(a2.embyid)

    def test_renew_code_extends_existing(self):
        setup_server()
        add_main(555, name="grace")
        patch_service(FakeService())
        open_account(FakeCall(555), "test", "t_grace", "pw", "9")
        before = sx.xacc_get("test", 555).ex
        sx.xcode_add(["T-test-XRNV_rn"], "test", 1, 10, "renew")
        run(xcode.xs_redeem_code(FakeMsg(555), "T-test-XRNV_rn"))
        after = sx.xacc_get("test", 555).ex
        self.assertTrue(after > before + timedelta(days=9))

    def test_renew_code_without_account_converts(self):
        setup_server()
        add_main(666, name="henry", embyid=None, lv="d")
        sx.xcode_add(["T-test-XRNV_cv"], "test", 1, 7, "renew")
        run(xcode.xs_redeem_code(FakeMsg(666), "T-test-XRNV_cv"))
        a = sx.xacc_get("test", 666)
        self.assertIsNotNone(a)
        self.assertEqual(int(a.us), 7)  # 转为资格，码不浪费


# ---------------- 管理面板入口 ----------------

class AdminPanelEntryTests(unittest.TestCase):
    def test_command_sends_panel_before_deleting_request(self):
        setup_server()
        msg = FakeMsg(1, "/xspanel", "admin")

        run(xa.xs_admin_cmd(None, msg))

        self.assertEqual(captured[0][0], "send")
        self.assertIn("测试服 · 管理", captured[0][1])
        self.assertEqual(captured[-1], ("delete",))
        self.assertFalse(any(item[0] == "edit" for item in captured))


# ---------------- 到期任务 ----------------

class ExpiryTests(unittest.TestCase):
    def test_expired_deleted_quota_released(self):
        setup_server()
        add_main(777, name="ivy")
        svc = FakeService()
        patch_service(svc)
        open_account(FakeCall(777), "test", "t_ivy", "pw", "9")
        self.assertEqual(sx.xquota_get("test").used, 1)
        sx.xacc_update("test", 777, ex=datetime.now() - timedelta(minutes=1))
        run(xserver_ex.check_xserver_expired())
        self.assertEqual(svc.deleted, ["ID-t_ivy"])
        self.assertIsNone(sx.xacc_get("test", 777))
        self.assertEqual(sx.xquota_get("test").used, 0)

    def test_grant_pending_row_not_touched(self):
        """仅有资格（embyid None, ex None）的行不受到期任务影响、不回收名额"""
        setup_server()
        add_main(888, name="jack", embyid=None, lv="d")
        sx.xacc_grant("test", 888, 15)
        run(xserver_ex.check_xserver_expired())
        a = sx.xacc_get("test", 888)
        self.assertIsNotNone(a)
        self.assertEqual(int(a.us), 15)

    def test_half_broken_cleaned(self):
        """embyid 有但 ex 为空的半成品 → 删除"""
        setup_server()
        add_main(900, name="kim")
        svc = FakeService()
        patch_service(svc)
        sx.xacc_add("test", 900, "ID-ghost", "t_kim", "p", "1", None)
        run(xserver_ex.check_xserver_expired())
        self.assertIn("ID-ghost", svc.deleted)
        self.assertIsNone(sx.xacc_get("test", 900))


# ---------------- 复开限制（防脚本蹲坑） ----------------

class ReopenTests(unittest.TestCase):
    def _open_then_expire(self, tg, name):
        add_main(tg, name=name, iv=250, embyid=None, lv='d')  # open 池用户，积分通道
        svc = FakeService()
        patch_service(svc)
        ok, out = open_account(FakeCall(tg), "test", f"t_{name}", None, "1", copied=False)
        self.assertTrue(ok, out)
        # 到期删除：名额回收，历史保留
        sx.xacc_update("test", tg, ex=datetime.now() - timedelta(minutes=1))
        run(xserver_ex.check_xserver_expired())
        self.assertEqual(sx.xquota_get("test").used_open, 0)  # 坑已回
        self.assertTrue(sx.xhist_has_opened("test", tg))
        return svc

    def test_reopen_blocked_when_disabled(self):
        setup_server(allow_reopen=False)
        self._open_then_expire(1001, "eve")
        # 回收的名额不能被他再抢回去
        ok, out = open_account(FakeCall(1001), "test", "t_eve2", None, "1", copied=False)
        self.assertFalse(ok)
        self.assertIn("只限体验一次", out)
        self.assertEqual(sx.xquota_get("test").used_open, 0)

    def test_reopen_allowed_when_enabled(self):
        setup_server(allow_reopen=True)
        self._open_then_expire(1002, "frank")
        ok, out = open_account(FakeCall(1002), "test", "t_frank2", None, "1", copied=False)
        self.assertTrue(ok, out)
        self.assertEqual(sx.xquota_get("test").used_open, 1)
        self.assertIsNotNone(sx.xacc_get("test", 1002))

    def test_grant_channel_exempt_from_lock(self):
        """管理员给回锅用户直发资格 → 绕过复开限制（定向放行），有效期=资格天数"""
        setup_server(allow_reopen=False)
        svc = self._open_then_expire(1003, "grace")
        sx.xacc_grant("test", 1003, 7)
        ok, out = open_account(FakeCall(1003), "test", "t_grace2", None, "1", copied=False)
        self.assertTrue(ok, out)
        self.assertEqual(svc.calls[-1][2], 7)  # days 用资格天数
        a = sx.xacc_get("test", 1003)
        self.assertIsNotNone(a.embyid)
        self.assertEqual(int(a.us), 0)

    def test_history_survives_cascade_and_expiry(self):
        """级联/到期删除都不抹历史：一人一次的原则跨通道成立"""
        setup_server(allow_reopen=False, total=5, open_total=5)
        add_main(1300, name="nick")
        svc = FakeService()
        patch_service(svc)
        ok, _ = open_account(FakeCall(1300), "test", "t_nick", "pw", "9")
        self.assertTrue(ok)
        # 主服被删 → 级联
        se.sql_update_emby(se.Emby.tg == 1300, embyid=None, name=None, lv='d')
        run(xserver_ex.check_xserver_expired())
        self.assertTrue(sx.xhist_has_opened("test", 1300))
        ok2, out2 = open_account(FakeCall(1300), "test", "t_nick2", "pw", "9")
        self.assertFalse(ok2)
        self.assertIn("只限体验一次", out2)

    def test_history_marks_count_and_first(self):
        setup_server(allow_reopen=True)
        add_main(1004, name="iris", iv=250, embyid=None, lv='d')
        patch_service(FakeService())
        open_account(FakeCall(1004), "test", "t_iris", None, "1", copied=False)
        run(xp.xs_del_confirm(None, FakeCall(1004, "xs:dc:" + xp._enc("test"))))
        open_account(FakeCall(1004), "test", "t_iris2", None, "1", copied=False)
        with sx.Session() as s:
            h = s.query(sx.XserverHistory).filter_by(server_id="test", tg=1004).first()
        self.assertEqual(h.open_count, 2)
        self.assertIsNotNone(h.first_cr)
        self.assertTrue(h.last_cr >= h.first_cr)


# ---------------- 主服软级联 ----------------

class CascadeTests(unittest.TestCase):
    def _open_main_pool(self, tg=1101, name="leo"):
        setup_server(total=3, open_total=3)
        add_main(tg, name=name, pwd="pw123", pwd2="9999")
        svc = FakeService()
        patch_service(svc)
        ok, out = open_account(FakeCall(tg), "test", f"t_{name}", "pw123", "9999")
        self.assertTrue(ok, out)
        return svc, tg

    def test_main_deleted_cascades_purge(self):
        svc, tg = self._open_main_pool()
        se.sql_update_emby(se.Emby.tg == tg, embyid=None, name=None, pwd=None,
                           pwd2=None, lv='d', cr=None, ex=None)   # 模拟 delme/rmemby 清空
        run(xserver_ex.check_xserver_expired())
        self.assertEqual(svc.deleted, [f"ID-t_leo"])
        self.assertIsNone(sx.xacc_get("test", tg))
        q = sx.xquota_get("test")
        self.assertEqual((q.used, q.used_open), (0, 0))  # main 池名额退回

    def test_main_banned_cascades_disable_only(self):
        svc, tg = self._open_main_pool(1102, "monk")
        se.sql_update_emby(se.Emby.tg == tg, lv='c')            # 模拟 check_ex 封印
        run(xserver_ex.check_xserver_expired())
        a = sx.xacc_get("test", tg)
        self.assertEqual(a.lv, 'c')                             # 测试号封印但保留
        self.assertEqual(svc.deleted, [])                       # 未删号
        self.assertEqual(sx.xquota_get("test").used, 1)         # 名额不退
        self.assertIn((f"ID-t_monk", True), svc.policy)         # disable=True
        # 重复跑幂等：已是 c 不再触发策略调用
        n = len(svc.policy)
        run(xserver_ex.check_xserver_expired())
        self.assertEqual(len(svc.policy), n)

    def test_main_healthy_untouched(self):
        svc, tg = self._open_main_pool(1103, "nora")
        run(xserver_ex.check_xserver_expired())
        self.assertEqual(svc.deleted, [])
        a = sx.xacc_get("test", tg)
        self.assertEqual(a.lv, 'b')
        self.assertEqual(sx.xquota_get("test").used, 1)

    def test_open_pool_immune(self):
        """open 池用户：主服无号是常态，绝不被级联误伤"""
        setup_server(total=3, open_total=3)
        add_main(1104, name="paul", iv=250, embyid=None, lv='d')
        svc = FakeService()
        patch_service(svc)
        ok, out = open_account(FakeCall(1104), "test", "t_paul", None, "1", copied=False)
        self.assertTrue(ok, out)
        run(xserver_ex.check_xserver_expired())
        self.assertEqual(svc.deleted, [])
        self.assertEqual(sx.xquota_get("test").used_open, 1)

    def test_grant_pending_row_immune(self):
        setup_server()
        add_main(1105, name="quinn", embyid=None, lv='d')
        sx.xacc_grant("test", 1105, 10)   # 资格行，无 embyid
        patch_service(FakeService())
        run(xserver_ex.check_xserver_expired())
        a = sx.xacc_get("test", 1105)
        self.assertIsNotNone(a)
        self.assertEqual(int(a.us), 10)

    def test_remote_delete_failure_keeps_row(self):
        svc, tg = self._open_main_pool(1106, "rita")

        async def fail_delete(eid):
            return False

        async def alive_user(eid):
            return True, {"Id": eid}       # 远端还在 → 不是 404

        svc.x_delete = fail_delete
        svc.user = alive_user
        se.sql_update_emby(se.Emby.tg == tg, embyid=None, lv='d')
        run(xserver_ex.check_xserver_expired())
        self.assertIsNotNone(sx.xacc_get("test", tg))           # 保留待下轮重试
        self.assertEqual(sx.xquota_get("test").used, 1)         # 名额未退


if __name__ == "__main__":
    unittest.main(verbosity=2)
