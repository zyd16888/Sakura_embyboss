# -*- coding: utf-8 -*-
"""
全模块图导入检查：模拟 main.py 的 import 链（不调用 bot.run()、不连真实 MySQL）。

- create_engine 被替换为 sqlite 内存引擎（pymysql URL 仅用于建 engine 对象）；
- SAKURA_RUNNING_MIGRATIONS=1 跳过 alembic（迁移 SQL 为 MySQL 方言）；
- 临时 config.json 含 xservers 样例，验证 Config 模型可加载新字段。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ["SAKURA_RUNNING_MIGRATIONS"] = "1"

import sqlalchemy  # noqa: E402
from sqlalchemy import create_engine as _real_ce  # noqa: E402

_engines = {}


def _patched_ce(url, *a, **k):
    s = str(url)
    if s.startswith("mysql"):
        key = s.split("@")[-1]
        if key not in _engines:
            _engines[key] = _real_ce("sqlite://")
        return _engines[key]
    return _real_ce(url, *a, **k)


sqlalchemy.create_engine = _patched_ce

CFG_PATH = ROOT / "config.json"
_tmp_created = not CFG_PATH.exists()
cfg = {
    "bot_name": "importcheckbot", "bot_token": "1:token", "owner_api": 1,
    "owner_hash": "h", "owner": 1, "group": [-1001], "main_group": "g",
    "chanel": "c", "bot_photo": "", "money": "花币",
    "emby_api": "k", "emby_url": "http://main:8096", "emby_line": "main.line",
    "db_host": "localhost", "db_user": "u", "db_pwd": "p", "db_name": "d",
    "open": {"stat": False, "all_user": 100, "checkin": True, "exchange": True,
             "whitelist": True, "invite": False, "leave_ban": True},
    "ranks": {"logo": "SAKURA"},
    "schedall": {},
    "xservers": [{
        "id": "test", "name": "测试服", "enable": True,
        "url": "http://test:8096", "api": "k2", "line": "http://test.line",
        "expire_days": 15, "all_user": 50, "block_libs": ["nsfw"],
        "name_prefix": "t_",
        "channels": {"main_user": True, "whitelist": False, "points": True,
                     "points_cost": 100},
    }],
}
CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

try:
    from bot import config as loaded_cfg  # noqa: E402
    assert loaded_cfg.xservers and loaded_cfg.xservers[0].id == "test"
    assert getattr(loaded_cfg.schedall, "xserver_check_ex", None) is True

    # main.py 的完整模块图
    from bot import bot  # noqa: E402
    from bot.modules.panel import *  # noqa: E402
    from bot.modules.commands import *  # noqa: E402
    from bot.modules.extra import *  # noqa: E402
    from bot.modules.callback import *  # noqa: E402

    # xserver 专属模块确认在图中
    import sys as _s
    mods = _s.modules
    for m in ("bot.modules.panel.xserver_panel", "bot.modules.panel.xserver_admin",
              "bot.modules.commands.xserver_code", "bot.scheduler.xserver_ex",
              "bot.func_helper.xserver", "bot.sql_helper.sql_xserver"):
        assert m in mods, f"缺少模块 {m}"

    # 面板按钮注入
    from bot.func_helper.fix_bottons import judge_start_ikb, members_ikb  # noqa: E402
    kb = members_ikb(account=True)
    dumped = json.dumps(kb, ensure_ascii=False, default=str)
    assert "xs:m" in dumped, "用户面板未注入测试服按钮"
    kb2 = judge_start_ikb(False, False)
    assert "xs:m" in json.dumps(kb2, ensure_ascii=False, default=str)

    # quota 种子 & registry
    # 手动建表（跳过了 alembic）
    from bot.sql_helper import engine as _eng, Base as _Base  # noqa: E402
    _Base.metadata.create_all(bind=_eng)

    from bot.func_helper.xserver import get_service  # noqa: E402
    from bot.sql_helper.sql_xserver import xquota_get  # noqa: E402
    svc = get_service("test")
    assert svc is not None and svc.server_id == "test"
    q = xquota_get("test")
    assert q is not None and q.total == 50, f"quota 种子失败: {q}"

    # 到期任务注册（scheduler import 时注册 interval job）
    from bot.func_helper.scheduler import scheduler  # noqa: E402
    job = scheduler.SCHEDULER.get_job("xserver_check_ex")
    assert job is not None, "xserver_check_ex 定时任务未注册"

    print("FULL-IMPORT-CHECK: OK")
finally:
    if _tmp_created:
        CFG_PATH.unlink(missing_ok=True)
