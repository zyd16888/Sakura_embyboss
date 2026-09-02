# -*- coding: utf-8 -*-
"""
xserver 离线测试引导：不装 Telegram/MySQL/Emby 也能真实 import 被测模块。

- 以桩 `bot` 包（__path__ 指向真实目录）注入全局名，
  真实 bot.schemas / bot.sql_helper.* / bot.func_helper.xserver 可导入；
- 以桩 pyrogram + bot.func_helper.filters/msg_utils 捕获面板逻辑；
- sqlite 内存库同时挂到 sql_xserver.Session 与 sql_emby.Session。
"""
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SAKURA_RUNNING_MIGRATIONS", "1")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class _Filter:
    def __init__(self, name='f'):
        self.name = name

    def __and__(self, other):
        return self

    def __or__(self, other):
        return self


def _regex(p):
    return _Filter(p)


def _command(*a, **k):
    return _Filter('command')


def _user(*a, **k):
    return _Filter('user')


def boot():
    """安装桩模块，返回命名空间 dict"""
    bot_stub = types.ModuleType("bot")
    bot_stub.__path__ = [str(ROOT / "bot")]
    bot_stub.LOGGER = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )

    class _Client:
        def on_message(self, *a, **k):
            def deco(fn):
                return fn
            return deco

        on_callback_query = on_message
        on_inline_query = on_message

        def __getattr__(self, name):
            async def _noop(*a, **k):
                return None
            return _noop

    cfg_obj = types.SimpleNamespace(xservers=[])  # 引用稳定，测试中改 .xservers
    bot_stub.config = cfg_obj
    bot_stub.save_config = lambda: None
    bot_stub.bot = _Client()
    bot_stub.bot_name = "testbot"
    bot_stub.bot_token = "1:test"
    bot_stub.owner_api = 1
    bot_stub.owner_hash = "h"
    bot_stub.owner = 1
    bot_stub.group = [-1001]
    bot_stub.main_group = "g"
    bot_stub.money = "花币"
    bot_stub.sakura_b = "花币"
    bot_stub.bot_photo = ""
    bot_stub.money = "花币"
    bot_stub.admins = []
    bot_stub.prefixes = ['/', '!', '.', '，', '。']
    bot_stub.ranks = {"logo": "T", "backdrop": False}
    bot_stub.schedall = types.SimpleNamespace(check_ex=True, low_activity=False,
                                              partition_check=False, xserver_check_ex=True)
    bot_stub._open = types.SimpleNamespace(stat=False, all_user=1000, tem=0, open_us=30,
                                           register_worker_count=5, register_queue_limit=100,
                                           exchange=True, exchange_cost=100, whitelist=True,
                                           use_whitelist_code=False, invite_lv="b", checkin=False)
    bot_stub.emby_api = "MAINKEY"
    bot_stub.emby_url = "http://main.example.com:8096"
    bot_stub.emby_line = "main.example.com"
    bot_stub.emby_whitelist_line = None
    bot_stub.emby_block = ["nsfw"]
    bot_stub.extra_emby_libs = []
    bot_stub.partition_libs = {}
    bot_stub.db_host = "localhost"
    bot_stub.db_user = "u"
    bot_stub.db_pwd = "p"
    bot_stub.db_name = "d"
    bot_stub.db_port = 3306
    bot_stub.db_is_docker = False
    bot_stub.db_docker_name = "mysql"
    bot_stub.db_backup_dir = "./db_backup"
    bot_stub.db_backup_maxcount = 7
    bot_stub.tz_ad = None
    bot_stub.tz_api = None
    bot_stub.tz_id = []
    bot_stub.tz_version = "v0"
    bot_stub.tz_username = None
    bot_stub.tz_password = None
    bot_stub.w_anti_channel_ids = []
    bot_stub.kk_gift_days = 30
    bot_stub.fuxx_pitao = False
    bot_stub.activity_check_days = 21
    bot_stub.red_envelope = types.SimpleNamespace(status=False, allow_private=False)
    bot_stub.moviepilot = types.SimpleNamespace(status=False)
    bot_stub.auto_update = types.SimpleNamespace(status=False, git_repo="", commit_sha=None)
    bot_stub.api = types.SimpleNamespace(status=False)
    bot_stub.proxy = types.SimpleNamespace(scheme="", dict=lambda: {})
    sys.modules["bot"] = bot_stub

    # pyrogram 桩（面板装饰器与按钮构建）
    filters_stub = types.SimpleNamespace(regex=_regex, command=_command, user=_user,
                                         private=_Filter('private'),
                                         create=lambda f: _Filter('create'))
    pyrogram_stub = types.ModuleType("pyrogram")
    pyrogram_stub.filters = filters_stub
    pyrogram_stub.enums = types.SimpleNamespace(ParseMode=types.SimpleNamespace(MARKDOWN="md"))
    pyrogram_stub.types = types.ModuleType("pyrogram.types")
    pyrogram_stub.types.BotCommand = lambda *a, **k: None
    pyrogram_stub.types.InlineKeyboardMarkup = object

    class CallbackQuery:
        pass

    pyrogram_stub.types.CallbackQuery = CallbackQuery
    pyrogram_stub.errors = types.ModuleType("pyrogram.errors")

    class _BadRequest(Exception):
        ID = ""

    pyrogram_stub.errors.BadRequest = _BadRequest
    pyrogram_stub.errors.FloodWait = type("FloodWait", (Exception,), {})
    pyrogram_stub.errors.Forbidden = type("Forbidden", (Exception,), {})
    pyrogram_stub.errors.PeerIdInvalid = type("PeerIdInvalid", (Exception,), {})
    pyrogram_stub.errors.ListenerTimeout = type("ListenerTimeout", (Exception,), {})
    helpers = types.ModuleType("pyrogram.helpers")
    helpers.ikb = lambda rows, **k: rows
    helpers.array_chunk = lambda lst, n: [lst[i:i + n] for i in range(0, len(lst), n)]
    sys.modules["pyrogram"] = pyrogram_stub
    sys.modules["pyrogram.filters"] = filters_stub
    sys.modules["pyrogram.types"] = pyrogram_stub.types
    sys.modules["pyrogram.errors"] = pyrogram_stub.errors
    sys.modules["pyrogram.enums"] = pyrogram_stub.enums
    sys.modules["pyrogram.helpers"] = helpers

    # func_helper.filters / msg_utils 桩
    fh_stub = types.ModuleType("bot.func_helper")
    fh_stub.__path__ = [str(ROOT / "bot" / "func_helper")]
    sys.modules["bot.func_helper"] = fh_stub

    filt_stub = types.ModuleType("bot.func_helper.filters")
    filt_stub.user_in_group_on_filter = _Filter("user_in_group")
    filt_stub.admins_on_filter = _Filter("admins_on")
    filt_stub.user_in_group_filter = lambda *a, **k: True
    filt_stub.user_in_group_f = filt_stub.user_in_group_on_filter
    filt_stub.admins_filter = _Filter("admins")
    sys.modules["bot.func_helper.filters"] = filt_stub

    msg_stub = types.ModuleType("bot.func_helper.msg_utils")
    captured = []

    async def sendMessage(message, text, buttons=None, timer=None, send=False, chat_id=None,
                          parse_mode=None):
        captured.append(("send", text, buttons, send))
        return True

    async def editMessage(message, text, buttons=None, timer=None, parse_mode=None):
        captured.append(("edit", text, buttons))
        return True

    async def callAnswer(call, query, show_alert=False):
        captured.append(("answer", query))
        return True

    async def deleteMessage(message, second=0):
        captured.append(("delete",))
        return True

    async def ask_return(update, text, timer=120, button=None):
        captured.append(("ask", text))
        return getattr(update, "_ask_reply", None)

    msg_stub.sendMessage = sendMessage
    msg_stub.editMessage = editMessage
    msg_stub.callAnswer = callAnswer
    msg_stub.deleteMessage = deleteMessage
    msg_stub.ask_return = ask_return
    msg_stub.sendPhoto = sendMessage
    msg_stub.callListen = lambda *a, **k: None
    sys.modules["bot.func_helper.msg_utils"] = msg_stub
    bot_stub.msg_captured = captured
    bot_stub.CallbackQuery = CallbackQuery

    # bot.modules / bot.scheduler 包桩：
    # 避免触发真实 __init__（admin_panel 等拉 PIL/requests/apscheduler 全家桶）
    for pkg in ("bot.modules", "bot.modules.panel", "bot.modules.commands", "bot.scheduler"):
        m = types.ModuleType(pkg)
        m.__path__ = [str(ROOT / pkg.replace(".", "/"))]
        sys.modules[pkg] = m

    from bot.schemas import Config, Xserver, XserverChannels  # noqa: F401  真实模型
    from bot.sql_helper import Base  # noqa: F401            真实 Base
    from bot.sql_helper import sql_xserver as sx
    from bot.sql_helper import sql_emby as se
    from bot.func_helper import xserver as xs

    return {"bot_stub": bot_stub, "cfg": cfg_obj, "sx": sx, "se": se, "xs": xs,
            "captured": captured, "Config": Config, "Xserver": Xserver,
            "XserverChannels": XserverChannels, "CallbackQuery": CallbackQuery}


def fresh_db(ns, extra_tables=()):
    """sqlite 内存库：xserver 四表 + emby 表；接管两个模块的 Session"""
    eng = create_engine("sqlite://")
    for t in (ns["sx"].XserverAccount, ns["sx"].XserverQuota, ns["sx"].XserverCode,
              ns["sx"].XserverHistory, ns["se"].Emby, *extra_tables):
        t.__table__.create(eng)
    sm = sessionmaker(bind=eng)
    ns["sx"].Session = sm
    ns["se"].Session = sm
    ns["xs"]._registry.clear()
    ns["xs"]._signature.clear()
    ns["captured"].clear()
    return sm
