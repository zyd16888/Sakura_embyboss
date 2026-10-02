"""
xserver 到期检测 + 主服软级联。每小时跑一次（schedall.xserver_check_ex）。

到期语义（用户确认）：到期 = 删除远端账号 + 清除本地记录 + 回收名额。

软级联（方案一，不碰任何主服原代码）：对 main 池（开号时主服有账号）的体验号，
对齐主服现状——
- 主服账号已被删/清空（delme / !rmemby / 退群清理 / check_ex 真删）→ 级联删除体验号并退名额；
- 主服被封印（lv='c'）→ 级联封印体验号（保留名额与到期，主服恢复后由管理员 !xsext 解封）；
- open 池（主服无号的体验用户）与未开号的资格行完全跳过。
"""
from datetime import datetime

from bot import bot, LOGGER
from bot.func_helper.xserver import (enabled_servers, get_service,
                                     notify_xserver_deleted)
from bot.sql_helper.sql_emby import sql_get_emby
from bot.sql_helper.sql_xserver import (xacc_all, xacc_delete, xacc_expired_rows,
                                        xacc_update, xquota_give, POOL_MAIN)


async def _remote_delete(svc, a) -> bool:
    """远端删号；404 视为已删"""
    deleted = await svc.x_delete(a.embyid)
    if not deleted:
        ok_u, uinfo = await svc.user(a.embyid)
        deleted = (not ok_u) and isinstance(uinfo, dict) and '资源不存在' in uinfo.get('error', '')
    return deleted


async def _purge(svc, xc, a, reason: str, notice: str) -> bool:
    """删远端 + 删本地行 + 按原池退名额 + 私聊通知。失败保留待下轮重试。"""
    if not await _remote_delete(svc, a):
        LOGGER.warning(f'【xserver{reason}】{xc.id} 远端删除失败，保留待重试 tg={a.tg}')
        return False
    if not xacc_delete(xc.id, a.tg):
        return False
    xquota_give(xc.id, a.pool or POOL_MAIN)
    LOGGER.info(f'【xserver{reason}】{xc.name} 已删除 `{a.name}` (tg={a.tg})，'
                f'名额已退回{a.pool or POOL_MAIN}')
    try:
        await bot.send_message(a.tg, notice)
    except Exception as e:
        LOGGER.warning(f'【xserver{reason}】通知用户失败 {a.tg}: {e}')
    await notify_xserver_deleted(xc, a.tg, reason)
    return True


async def check_xserver_expired():
    """小时任务 = 到期清理 + 主服级联，两段串行"""
    servers = enabled_servers()
    if not servers:
        return
    await _expire_pass(servers)
    await _cascade_pass(servers)


async def _expire_pass(servers):
    now = datetime.now()
    for xc in servers:
        svc = get_service(xc.id)
        if svc is None:
            continue
        rows = xacc_expired_rows(xc.id, now)
        for a in rows:
            # DAO 保证返回行必有 embyid（到期行 / 已建号无期限的半成品）
            await _purge(svc, xc, a, '到期自动删除',
                         f'🧪 你的 {xc.name} 体验账号 `{a.name}` 已到期删除，'
                         f'名额已释放。欢迎下次再来体验～')


async def _cascade_pass(servers):
    """主服状态软级联（仅 main 池、已开号的行）"""
    for xc in servers:
        svc = get_service(xc.id)
        if svc is None:
            continue
        for a in xacc_all(xc.id):
            if not a.embyid:
                continue  # 未开号的资格行
            if (a.pool or POOL_MAIN) != POOL_MAIN:
                continue  # open 池体验用户与主服无关
            main = sql_get_emby(a.tg)
            if main is None or not main.embyid:
                await _purge(svc, xc, a, '主服账号清理后同步删除',
                             f'🧪 你的主服账号已被删除/清理，{xc.name} 体验账号 '
                             f'`{a.name}` 已同步删除，名额已释放。')
            elif main.lv == 'c' and a.lv == 'b':
                if await svc.emby_change_policy(a.embyid, disable=True):
                    xacc_update(xc.id, a.tg, lv='c')
                    LOGGER.info(f'【xserver主服级联】{xc.name} 封印 `{a.name}` '
                                f'(tg={a.tg})：主服已被封')
                    try:
                        await bot.send_message(
                            a.tg, f'🧪 你的主服账号已被封禁，{xc.name} 体验账号 '
                                  f'`{a.name}` 已同步封印。主服恢复后可联系管理员解封。')
                    except Exception as e:
                        LOGGER.warning(f'【xserver主服级联】通知失败 {a.tg}: {e}')


# 启动时注册：每小时检测一次体验服到期+级联（schedall.xserver_check_ex 控制开关）
def _register_job():
    try:
        from bot import schedall
        if not getattr(schedall, 'xserver_check_ex', True):
            return
        from bot.func_helper.scheduler import scheduler
        scheduler.add_job(check_xserver_expired, 'interval', hours=1, id='xserver_check_ex')
    except Exception as e:
        LOGGER.error(f'【xserver到期检测】定时任务注册失败: {e}')


_register_job()
