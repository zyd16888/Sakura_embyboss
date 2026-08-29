"""
xserver 到期检测：到期 = 删除远端账号 + 清除本地记录 + 回收名额（用户确认的语义）。

由 schedall.xserver_check_ex 控制，每小时跑一次；
数据访问全部走 bot.sql_helper.sql_xserver（可被测试接管 Session）。
"""
from datetime import datetime

from bot import bot, LOGGER
from bot.func_helper.xserver import enabled_servers, get_service
from bot.sql_helper.sql_xserver import (xacc_delete, xacc_expired_rows, xquota_give)


async def check_xserver_expired():
    servers = enabled_servers()
    if not servers:
        return
    now = datetime.now()
    for xc in servers:
        svc = get_service(xc.id)
        if svc is None:
            continue
        rows = xacc_expired_rows(xc.id, now)
        if not rows:
            continue
        for a in rows:
            deleted = True
            if a.embyid:
                deleted = await svc.x_delete(a.embyid)
                if not deleted:
                    # 远端查不到视为已删；真实失败则保留记录下轮重试
                    ok_u, uinfo = await svc.user(a.embyid)
                    gone = (not ok_u) and isinstance(uinfo, dict) and '资源不存在' in uinfo.get('error', '')
                    deleted = gone
            if not deleted:
                LOGGER.warning(f'【xserver到期检测】{xc.id} 删除失败，保留待重试 tg={a.tg}')
                continue
            if xacc_delete(xc.id, a.tg):
                # 只有真实开过号的行才回收名额（资格待用行 embyid 为空）
                if a.embyid:
                    xquota_give(xc.id)
                LOGGER.info(f'【xserver到期检测】{xc.name} 到期删除 [{a.name}](tg={a.tg}) Done！')
                try:
                    await bot.send_message(a.tg, f'🧪 你的 {xc.name} 测试账号 `{a.name}` 已到期删除，'
                                                 f'名额已释放。欢迎下次再来体验～')
                except Exception as e:
                    LOGGER.warning(f'【xserver到期检测】通知用户失败 {a.tg}: {e}')


# 启动时注册：每小时检测一次测试服到期（schedall.xserver_check_ex 控制开关）
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
