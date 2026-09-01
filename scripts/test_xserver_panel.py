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
        self.command = text.lstrip('/!.，。').split()

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
    """所有 xserver 模块均用直接导入，测试时同步替换服务实例。"""
    xp.get_service = lambda sid: svc
    xa.get_service = lambda sid: svc
    xs.get_service = lambda sid: svc
    xserver_ex.get_service = lambda sid: svc


def run(coro):
    return asyncio.run(coro)


def open_account(call, sid, name, pwd, pwd2, copied=True):
    return run(xp._occupy_and_open(call, sid, name, pwd, pwd2, copied=copied))


def group_notices():
    return [item[1] for item in captured
            if item[0] == "send" and len(item) > 3 and item[3] is True]


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


# ---------------- 用户面板展示 ----------------

class UserPanelDisplayTests(unittest.TestCase):
    def test_existing_account_shows_login_password(self):
        setup_server()
        add_main(111, name="alice", pwd="pw123", pwd2="9999")
        patch_service(FakeService())
        ok, out = open_account(FakeCall(111), "test", "t_alice", "pw123", "9999")
        self.assertTrue(ok, out)

        run(xp.xs_home_show(None, FakeCall(111), "test"))

        panel = next(item for item in reversed(captured) if item[0] == "edit")
        self.assertIn("· 登录密码 | `pw123`", panel[1])

    def test_offer_shows_duration_cost_balance_and_once_only_rule(self):
        setup_server(allow_reopen=False)
        add_main(116, name="pending", embyid=None, lv="d", iv=250)
        patch_service(FakeService())

        run(xp.xs_home_show(None, FakeCall(116), "test"))

        panel = next(item for item in reversed(captured) if item[0] == "edit")
        self.assertIn("· 本次有效期 | `15 天`", panel[1])
        self.assertIn("扣除 100花币（余额 250 → 150）", panel[1])
        self.assertIn("每人一次", panel[1])
        buttons = [button for row in panel[2] for button in row]
        self.assertTrue(any("100花币 · 15天" in label for label, _ in buttons))

    def test_insufficient_points_disables_open_buttons_and_uses_currency_name(self):
        setup_server()
        add_main(112, name="poor", embyid=None, lv="d", iv=0)
        patch_service(FakeService())
        currency_name = "金币"
        original_currency_name = xp.sakura_b
        xp.sakura_b = currency_name
        try:
            run(xp.xs_home_show(None, FakeCall(112), "test"))
        finally:
            xp.sakura_b = original_currency_name

        panel = next(item for item in reversed(captured) if item[0] == "edit")
        buttons = [button for row in panel[2] for button in row]
        self.assertIn(
            (f"🚫 {currency_name}不足 · 需要 100（持有 0）", f"xs:h:{xp._enc('test')}"),
            buttons,
        )
        self.assertFalse(any(data.startswith(("xs:c:", "xs:q:")) for _, data in buttons))


# ---------------- 开号流程 ----------------

class OpenFlowTests(unittest.TestCase):
    def test_quick_copy_credentials(self):
        setup_server()
        add_main(111, name="alice", pwd="SecretPass!", pwd2="SafeCode987")
        svc = FakeService()
        patch_service(svc)
        ok, out = open_account(
            FakeCall(111), "test", "t_alice", "SecretPass!", "SafeCode987")
        self.assertTrue(ok, out)
        self.assertEqual(svc.calls[0], ("create", "t_alice", 15, "SecretPass!"))
        a = sx.xacc_get("test", 111)
        self.assertEqual((a.name, a.pwd, a.pwd2, a.lv),
                         ("t_alice", "SecretPass!", "SafeCode987", "b"))
        self.assertEqual(sx.xquota_get("test").used, 1)
        self.assertIn("开号成功", out)
        self.assertIn("本次有效期 | `15 天`", out)
        self.assertIn("开通方式 | 免费", out)
        notices = group_notices()
        self.assertEqual(notices, [
            "🎉 恭喜 [这位小伙伴](tg://user?id=111) 成功开通「测试服」账号！"
        ])
        self.assertNotIn("\n", notices[0])
        self.assertNotIn("t_alice", notices[0])
        self.assertNotIn("SecretPass!", notices[0])
        self.assertNotIn("SafeCode987", notices[0])

    def test_failed_open_does_not_send_group_notice(self):
        setup_server()
        add_main(119, name="failed")
        patch_service(FakeService(create_ok=False))

        ok, _ = open_account(
            FakeCall(119), "test", "t_failed", "SecretPass!", "SafeCode987")

        self.assertFalse(ok)
        self.assertEqual(group_notices(), [])

    def test_quick_open_previews_terms_without_creating(self):
        setup_server(allow_reopen=False)
        add_main(117, name="preview", iv=250)
        svc = FakeService()
        patch_service(svc)

        run(xp.xs_quick(None, FakeCall(117, f"xs:q:{xp._enc('test')}")))

        self.assertEqual(svc.calls, [])
        self.assertIsNone(sx.xacc_get("test", 117))
        self.assertEqual(se.sql_get_emby(117).iv, 250)
        preview = next(item for item in reversed(captured) if item[0] == "edit")
        self.assertIn("确认开通", preview[1])
        self.assertIn("本次有效期 | `15 天`", preview[1])
        self.assertIn("开通方式 | 免费", preview[1])
        self.assertIn("每人一次", preview[1])
        self.assertIn(("✅ 确认开号", f"xs:qc:{xp._enc('test')}"), preview[2][0])

    def test_quick_confirm_reports_points_charge_and_processing(self):
        xc = setup_server()
        xc.channels.main_user = False
        add_main(118, name="paid", iv=250)
        svc = FakeService()
        patch_service(svc)

        run(xp.xs_quick(None, FakeCall(118, f"xs:q:{xp._enc('test')}")))
        self.assertEqual(se.sql_get_emby(118).iv, 250)
        run(xp.xs_quick_confirm(None, FakeCall(118, f"xs:qc:{xp._enc('test')}")))

        self.assertEqual(se.sql_get_emby(118).iv, 150)
        self.assertEqual(svc.calls[0][:3], ("create", "t_paid", 15))
        edits = [item for item in captured if item[0] == "edit"]
        self.assertTrue(any("正在创建" in item[1] for item in edits))
        self.assertIn("已扣 `100`", edits[-1][1])
        self.assertIn("余额 `250 → 150`", edits[-1][1])

    def test_create_accepts_default_underscore_prefix(self):
        xc = setup_server()
        add_main(114, name="pending", embyid=None, lv="d", iv=500)
        svc = FakeService()
        patch_service(svc)
        call = FakeCall(114, f"xs:c:{xp._enc('test')}")
        call._ask_reply = FakeMsg(114, "aluminum 1234")

        run(xp.xs_create(None, call))

        self.assertEqual(xc.name_prefix, "t_")
        self.assertEqual(svc.calls[0][:3], ("create", "t_aluminum", 15))
        self.assertTrue(svc.calls[0][3])
        account = sx.xacc_get("test", 114)
        self.assertEqual((account.name, account.pwd2), ("t_aluminum", "1234"))
        prompt = next(item for item in captured if item[0] == "ask")
        self.assertIn("本次有效期 | `15 天`", prompt[1])
        self.assertIn("扣除 100花币（余额 500 → 400）", prompt[1])
        self.assertTrue(any(item[0] == "reply" and "正在初始化" in item[1]
                            for item in captured))
        result = next(item for item in reversed(captured) if item[0] == "edit")
        self.assertIn("已扣 `100`", result[1])
        self.assertIn("余额 `500 → 400`", result[1])

    def test_create_rejects_special_character_before_service_call(self):
        setup_server()
        add_main(115, name="pending", embyid=None, lv="d", iv=500)
        svc = FakeService()
        patch_service(svc)
        call = FakeCall(115, f"xs:c:{xp._enc('test')}")
        call._ask_reply = FakeMsg(115, "bad_name 1234")

        run(xp.xs_create(None, call))

        self.assertEqual(svc.calls, [])
        reply = next(item for item in reversed(captured) if item[0] == "reply")
        self.assertIn("用户名含空格或特殊字符", reply[1])

    def test_create_rejects_insufficient_points_before_prompt(self):
        setup_server()
        add_main(113, name="poor", embyid=None, lv="d", iv=0)
        patch_service(FakeService())
        currency_name = "金币"
        original_currency_name = xp.sakura_b
        xp.sakura_b = currency_name
        try:
            run(xp.xs_create(None, FakeCall(113, f"xs:c:{xp._enc('test')}")))
        finally:
            xp.sakura_b = original_currency_name

        self.assertFalse(any(item[0] == "ask" for item in captured))
        answer = next(item for item in reversed(captured) if item[0] == "answer")
        self.assertEqual(
            answer[1], f"💦 {currency_name}不足，开号需要 100{currency_name}，当前 0")

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
        self.assertIn("已退回 `100花币`", out2)
        self.assertIn("余额已恢复", out2)
        self.assertEqual(se.sql_get_emby(222).iv, 150)
        q = sx.xquota_get("test")
        self.assertEqual((q.used, q.used_open), (0, 0))

    def test_rollback_failure_does_not_claim_refund_success(self):
        setup_server()
        add_main(223, name="rollback", iv=250, embyid=None, lv='d')
        patch_service(FakeService(create_ok=False))
        original_give = xp.xquota_give
        xp.xquota_give = lambda *args, **kwargs: False
        try:
            ok, out = open_account(
                FakeCall(223), "test", "t_rollback", None, "1234", copied=False)
        finally:
            xp.xquota_give = original_give

        self.assertFalse(ok)
        self.assertIn("自动回滚未完全成功", out)
        self.assertNotIn("已退回 `100花币`", out)
        self.assertEqual(se.sql_get_emby(223).iv, 250)

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
        captured.clear()
        run(xp.xs_del_confirm(None, FakeCall(111, "xs:dc:" + xp._enc("test"))))
        self.assertEqual(svc.deleted, ["ID-t_alice"])
        self.assertIsNone(sx.xacc_get("test", 111))
        self.assertEqual(sx.xquota_get("test").used, 0)
        self.assertEqual(group_notices(), [
            "🗑️ [这位小伙伴](tg://user?id=111) 的「测试服」账号已删除"
            "（用户主动注销），名额已释放。"
        ])
        self.assertNotIn("t_alice", group_notices()[0])


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
        xc.channels.points = True       # 积分通道开启时，资格仍优先且不扣币
        add_main(444, name="frank", embyid=None, lv="d")
        balance_before = se.sql_get_emby(444).iv
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
        self.assertIn("已使用 `30 天` 开号资格", out)
        a2 = sx.xacc_get("test", 444)
        self.assertEqual(int(a2.us), 0)
        self.assertIsNotNone(a2.embyid)
        self.assertEqual(se.sql_get_emby(444).iv, balance_before)

    def test_code_list_filters_kind_status_and_pages(self):
        setup_server()
        reg_codes = [f"T-test-XREG_{i:02d}" for i in range(12)]
        self.assertTrue(sx.xcode_add(reg_codes, "test", 1, 30, "reg"))
        self.assertTrue(sx.xcode_add(["T-test-XRNV_only"], "test", 1, 7, "renew"))
        self.assertTrue(sx.xcode_redeem(reg_codes[0], 7001)[0])
        self.assertTrue(sx.xcode_redeem(reg_codes[1], 7002)[0])

        unused, unused_total = sx.xcode_list("test", "reg", False, 0, 5)
        unused_page_2, _ = sx.xcode_list("test", "reg", False, 1, 5)
        used, used_total = sx.xcode_list("test", "reg", True, 0, 10)
        renew, renew_total = sx.xcode_list("test", "renew", False, 0, 10)

        self.assertEqual((unused_total, len(unused), len(unused_page_2)), (10, 5, 5))
        self.assertTrue(all(row.kind == "reg" and row.used is None for row in unused))
        self.assertEqual((used_total, {row.used for row in used}), (2, {7001, 7002}))
        self.assertEqual((renew_total, [row.code for row in renew]),
                         (1, ["T-test-XRNV_only"]))

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

    def test_credit_failure_restores_code(self):
        setup_server()
        add_main(667, name="ida", embyid=None, lv="d")
        code = "T-test-XREG_restore"
        sx.xcode_add([code], "test", 1, 7, "reg")
        original_grant = xcode.xacc_grant
        xcode.xacc_grant = lambda *args, **kwargs: False
        try:
            handled = run(xcode.xs_redeem_code(FakeMsg(667), code))
        finally:
            xcode.xacc_grant = original_grant

        self.assertTrue(handled)
        self.assertIsNone(sx.xcode_get(code).used)
        self.assertTrue(any("注册码未消耗" in str(item) for item in captured))


# ---------------- 管理面板入口 ----------------

class AdminPanelEntryTests(unittest.TestCase):
    def test_name_prefix_validator_allows_default_and_enforces_length(self):
        validator = xa._SET_HINTS["name_prefix"][2]

        self.assertTrue(validator("t_"))
        self.assertIn("8字符", validator("123456789"))

    def test_command_sends_panel_before_deleting_request(self):
        setup_server()
        msg = FakeMsg(1, "/xspanel", "admin")

        run(xa.xs_admin_cmd(None, msg))

        self.assertEqual(captured[0][0], "send")
        self.assertIn("测试服 · 管理", captured[0][1])
        self.assertEqual(captured[-1], ("delete",))
        self.assertFalse(any(item[0] == "edit" for item in captured))

    def test_code_manager_filters_used_and_unused(self):
        setup_server()
        codes = ["T-test-XREG_unused", "T-test-XREG_used"]
        self.assertTrue(sx.xcode_add(codes, "test", 1, 30, "reg"))
        self.assertTrue(sx.xcode_redeem(codes[1], 8001)[0])
        sid_hex = xp._enc("test")

        run(xa.xs_admin_code_list(
            None, FakeCall(1, f"xsa:cl:{sid_hex}:reg:unused:0")))

        unused_view = next(item for item in reversed(captured) if item[0] == "edit")
        self.assertIn("测试服 · 开号码", unused_view[1])
        self.assertIn(codes[0], unused_view[1])
        self.assertNotIn(codes[1], unused_view[1])
        buttons = [button for row in unused_view[2] for button in row]
        self.assertIn(("已使用", f"xsa:cl:{sid_hex}:reg:used:0"), buttons)
        self.assertIn(("➕ 生成开号码", f"xsa:cr:{sid_hex}"), buttons)

        captured.clear()
        run(xa.xs_admin_code_list(
            None, FakeCall(1, f"xsa:cl:{sid_hex}:reg:used:0")))

        used_view = next(item for item in reversed(captured) if item[0] == "edit")
        self.assertIn(codes[1], used_view[1])
        self.assertNotIn(codes[0], used_view[1])
        self.assertIn("tg://user?id=8001", used_view[1])

    def test_grant_command_replies_before_deleting_request(self):
        setup_server()
        add_main(2001, name="grant-user", embyid=None, lv="d")
        msg = FakeMsg(1, "/xsgr 2001 7", "admin")

        run(xa.xs_admin_grant(None, msg))

        self.assertEqual(int(sx.xacc_get("test", 2001).us), 7)
        self.assertEqual(captured[0][0], "send")
        self.assertEqual(captured[-1], ("delete",))

    def test_remove_aborts_without_remote_service(self):
        setup_server()
        add_main(2002, name="remove-user")
        svc = FakeService()
        patch_service(svc)
        open_account(FakeCall(2002), "test", "t_remove", "pw", "9")
        xa.get_service = lambda sid: None
        msg = FakeMsg(1, "/xsrm 2002", "admin")

        run(xa.xs_admin_remove(None, msg))

        self.assertIsNotNone(sx.xacc_get("test", 2002))
        self.assertEqual(sx.xquota_get("test").used, 1)
        self.assertEqual(svc.deleted, [])
        self.assertTrue(any("避免遗留远端账号" in str(item) for item in captured))

    def test_remove_command_sends_sanitized_group_notice(self):
        setup_server()
        add_main(2004, name="remove-success")
        svc = FakeService()
        patch_service(svc)
        open_account(FakeCall(2004), "test", "t_remove_success", "pw", "9")
        captured.clear()

        run(xa.xs_admin_remove(None, FakeMsg(1, "/xsrm 2004", "admin")))

        self.assertEqual(svc.deleted, ["ID-t_remove_success"])
        self.assertEqual(group_notices(), [
            "🗑️ [这位小伙伴](tg://user?id=2004) 的「测试服」账号已删除"
            "（管理员删除），名额已释放。"
        ])
        self.assertNotIn("t_remove_success", group_notices()[0])

    def test_panel_delete_requires_confirmation(self):
        setup_server()
        add_main(2003, name="delete-user")
        svc = FakeService()
        patch_service(svc)
        open_account(FakeCall(2003), "test", "t_delete", "pw", "9")
        sid_hex = xp._enc("test")

        run(xa.xs_admin_delete_ask(None, FakeCall(1, f"xsa:ox:{sid_hex}:2003")))

        self.assertIsNotNone(sx.xacc_get("test", 2003))
        self.assertEqual(svc.deleted, [])
        self.assertTrue(any("确认删除测试服账号" in str(item) for item in captured))

        captured.clear()
        run(xa.xs_admin_delete_cb(None, FakeCall(1, f"xsa:oxc:{sid_hex}:2003")))

        self.assertIsNone(sx.xacc_get("test", 2003))
        self.assertEqual(svc.deleted, ["ID-t_delete"])
        self.assertEqual(sx.xquota_get("test").used, 0)
        self.assertEqual(group_notices(), [
            "🗑️ [这位小伙伴](tg://user?id=2003) 的「测试服」账号已删除"
            "（管理员删除），名额已释放。"
        ])
        self.assertNotIn("t_delete", group_notices()[0])


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
        captured.clear()
        run(xserver_ex.check_xserver_expired())
        self.assertEqual(svc.deleted, ["ID-t_ivy"])
        self.assertIsNone(sx.xacc_get("test", 777))
        self.assertEqual(sx.xquota_get("test").used, 0)
        self.assertEqual(group_notices(), [
            "🗑️ [这位小伙伴](tg://user?id=777) 的「测试服」账号已删除"
            "（到期自动删除），名额已释放。"
        ])
        self.assertNotIn("t_ivy", group_notices()[0])

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
        captured.clear()
        run(xserver_ex.check_xserver_expired())
        self.assertEqual(svc.deleted, [f"ID-t_leo"])
        self.assertIsNone(sx.xacc_get("test", tg))
        q = sx.xquota_get("test")
        self.assertEqual((q.used, q.used_open), (0, 0))  # main 池名额退回
        self.assertEqual(group_notices(), [
            f"🗑️ [这位小伙伴](tg://user?id={tg}) 的「测试服」账号已删除"
            "（主服账号清理后同步删除），名额已释放。"
        ])
        self.assertNotIn("t_leo", group_notices()[0])

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
        captured.clear()
        run(xserver_ex.check_xserver_expired())
        self.assertIsNotNone(sx.xacc_get("test", tg))           # 保留待下轮重试
        self.assertEqual(sx.xquota_get("test").used, 1)         # 名额未退
        self.assertEqual(group_notices(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
