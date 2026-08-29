"""
xserver 注册码兑换入口（独立于主服 exchange.py，不动 Rcode 表）。

码格式（对齐主服 ranks.logo 风格）：
- {LOGO}-{server_id}-XREG_{rand10}  → 开号资格码（兑换后获得 us 天开号资格）
- {LOGO}-{server_id}-XRNV_{rand10}  → 续期码（兑换后已有测试号到期时间 +us 天）

start.py 的分发判定是 `u in f'{ranks.logo}'`，码内含 LOGO 故天然可达；
本模块优先按 -XREG_/-XRNV_ 认领，非 xserver 码直接 return False 交回原逻辑。
"""
from bot import LOGGER
from bot.func_helper.msg_utils import sendMessage
from bot.sql_helper.sql_xserver import (xcode_get, xcode_redeem, xacc_get,
                                        xacc_update, xacc_grant)

XREG_MARK = '-XREG_'
XRNV_MARK = '-XRNV_'


def is_xserver_code(code: str) -> bool:
    return bool(code) and (XREG_MARK in code or XRNV_MARK in code)


async def xs_redeem_code(msg, code: str) -> bool:
    """
    尝试作为 xserver 码消化。
    :return: True 已处理（成功/失败均已提示）；False 非 xserver 码。
    """
    if not is_xserver_code(code):
        return False
    row = xcode_get(code)
    if row is None:
        # 形似 xserver 码但不存在：也要截获，避免落回主服注册码逻辑误报
        return await _fail(msg, code, '⛔ **无效的测试服注册码，请确认后重试。**')
    tg = msg.from_user.id
    ok, server_id, days = xcode_redeem(code, tg)
    if not ok:
        if row.used:
            return await _fail(msg, code, f'此 `{code}` \n测试服码已被使用，'
                                          f'是 [{row.used}](tg://user?id={row.used}) 的形状了喔')
        return await _fail(msg, code, '⚠️ 兑换失败，请稍后重试。')

    if XREG_MARK in code:
        a = xacc_get(server_id, tg)
        if a and a.embyid:
            # 已有测试号 → 资格入账，留作下次开号使用
            xacc_update(server_id, tg, us=int(a.us or 0) + days)
            await sendMessage(msg, f'🎉 你在 `{server_id}` 已有测试账号，'
                                   f'本码 {days} 天资格已入账（累计 {int(a.us or 0) + days} 天），'
                                   f'下次开号时优先使用。')
        else:
            xacc_grant(server_id, tg, days)
            await sendMessage(msg, f'🎉 已获得测试服 `{server_id}` **开号资格 {days} 天**！\n'
                                   f'前往【🧪 测试服开号】使用，用户名将自动加前缀。')
    else:
        a = xacc_get(server_id, tg)
        if not a or not a.embyid:
            # 续期码但没有可续的号：转为资格，不让码作废
            xacc_grant(server_id, tg, days)
            await sendMessage(msg, f'🔔 你没有 `{server_id}` 的测试账号，'
                                   f'该续期码 {days} 天已转为**开号资格**。')
        else:
            from datetime import datetime, timedelta
            base = a.ex if (a.ex and a.ex > datetime.now() and a.lv == 'b') else datetime.now()
            new_ex = base + timedelta(days=days)
            xacc_update(server_id, tg, ex=new_ex)
            await sendMessage(msg, f'🎊 `{server_id}` 测试号已续期 {days} 天\n'
                                   f'新到期时间：{new_ex}')
    masked = code[:-7] + '░' * 7
    LOGGER.info(f'【xserver码】{msg.from_user.first_name}[{tg}] 使用 {code}')
    await sendMessage(msg, f'· 🧪 测试服码使用 - [{msg.from_user.first_name}]'
                          f'(tg://user?id={tg}) 使用了 {masked}', send=True)
    return True


async def _fail(msg, code, text):
    LOGGER.info(f'【xserver码】无效尝试 {msg.from_user.id}: {code}')
    await sendMessage(msg, text, timer=60)
    return True
