"""
xserver（测试服等旁路扩展服务器）用户面板。

与主服 member_panel / emby 表完全隔离：
- 账号只读写 xserver_account / xserver_quota / xserver_code；
- 唯一触碰主服数据的地方是「积分通道扣 emby.iv」（复用现有字段，不改结构）。

双名额池：
- POOL_MAIN（config.all_user）：开号时主服已有账号的用户；
- POOL_OPEN（config.all_user_open）：开号时主服没有账号的纯测试用户。
账号行记录 pool，注销/到期/管理删除按原池回收。

回调格式 xs:<hex(server_id)>:<action>：
server_id 做 utf-8 hex 编码，数据仅含 0-9a-f 与前缀，不会命中任何既有
非锚定 handler 正则（'server'/'create'/'members'/'delme' 等均含非十六进制字符）。
"""
import re
from datetime import datetime, timedelta

from pyrogram import filters
from pyromod.helpers import ikb

from bot import bot, sakura_b, config, LOGGER
from bot.func_helper.concurrency import get_user_lock
from bot.func_helper.filters import user_in_group_on_filter
from bot.func_helper.msg_utils import callAnswer, editMessage, sendMessage, ask_return
from bot.func_helper.utils import pwd_create
from bot.func_helper.xserver import (get_service, enabled_servers,
                                     notify_xserver_opened, notify_xserver_deleted)
from bot.schemas import Xserver
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby
from bot.sql_helper.sql_xserver import (xacc_get, xacc_get_any, xacc_add, xacc_update,
                                        xacc_delete, xquota_get, xquota_take, xquota_give,
                                        xquota_pool_used, xquota_pool_total,
                                        xhist_mark, xhist_has_opened,
                                        POOL_MAIN, POOL_OPEN)

# 用户名禁用字符：空白 + markdown/Emby 敏感符号
_BAD_CHARS = re.compile(r'[\s~`*_\[\]()#+\\|<>:/?@&%$^{}=";,\']')
_NAME_MAX = 48


def _username_reject_reason(raw_name: str, prefix: str):
    if _BAD_CHARS.search(raw_name):
        return '用户名含空格或特殊字符'
    if len(f"{prefix}{raw_name}") > _NAME_MAX:
        return f'添加前缀后的用户名不能超过 {_NAME_MAX} 个字符'
    return None


# ---------------- 编解码 & 配置 ----------------

def _enc(sid: str) -> str:
    return sid.encode("utf-8").hex()


def _dec(hexed: str) -> str:
    return bytes.fromhex(hexed).decode("utf-8")


def _xc_of(sid: str):
    for xc in config.xservers or []:
        if xc.id == sid and xc.enable:
            return xc
    return None


def _pool_of(main) -> str:
    """按开号时主服状态选池：有账号→main池，无账号→open池"""
    return POOL_MAIN if (main is not None and main.embyid) else POOL_OPEN


def xserver_available() -> bool:
    """是否存在启用的扩展服务器（面板按钮注入判定）"""
    return bool(enabled_servers())


# ---------------- 资格判定 ----------------

def _eligibility(xc: Xserver, main, a):
    """
    :param main: 主服 Emby 行（可为 None）
    :param a:    该服务器的 xserver_account 行（可为 None，embyid 空但 us>0 = 持资格）
    :return: (kind, cost) kind ∈ free|grant|points|none
    """
    ch = xc.channels
    if main is not None and main.embyid:
        if ch.whitelist and main.lv == 'a':
            return 'free', 0
        if ch.main_user:
            return 'free', 0
    if a is not None and int(a.us or 0) > 0:
        return 'grant', 0
    if ch.points and main is not None:
        return 'points', ch.points_cost
    return 'none', 0


def _channel_text(xc: Xserver, main) -> str:
    ch = xc.channels
    iv = int(getattr(main, 'iv', 0) or 0)
    return '\n'.join([
        f"{'✅' if ch.main_user else '⛔'} 主服有效用户 · 免费一键",
        f"{'✅' if ch.whitelist else '⛔'} 白名单用户 · 免费",
        f"{'✅' if ch.points else '⛔'} 积分兑换 · {ch.points_cost}{sakura_b}（你持有 {iv}）",
    ])


def _fmt_cost(kind: str, cost: int) -> str:
    if kind == 'free':
        return ' · 免费'
    if kind == 'grant':
        return ' · 使用资格'
    return f' · {cost}{sakura_b}'


def _offer_days(xc: Xserver, kind: str, account) -> int:
    if kind == 'grant' and account is not None:
        return int(account.us or 0)
    return int(xc.expire_days)


def _offer_text(xc: Xserver, kind: str, cost: int, balance: int, account) -> str:
    days = _offer_days(xc, kind, account)
    if kind == 'free':
        method = '免费'
    elif kind == 'grant':
        method = f'使用 {days} 天开号资格'
    elif balance >= cost:
        method = f'扣除 {cost}{sakura_b}（余额 {balance} → {balance - cost}）'
    else:
        method = f'需要 {cost}{sakura_b}（当前 {balance}，余额不足）'

    lines = [
        f'· 本次有效期 | `{days} 天`',
        f'· 开通方式 | {method}',
        '· 到期处理 | 自动删除账号并回收名额',
    ]
    if kind != 'grant' and not xc.allow_reopen:
        lines.append('· 体验规则 | 每人一次，注销或到期后不能自行复开')
    return '\n'.join(lines)


def _fmt_offer(xc: Xserver, kind: str, cost: int, account) -> str:
    return f'{_fmt_cost(kind, cost)} · {_offer_days(xc, kind, account)}天'


def _result_buttons(sid: str, retry: str = None):
    rows = []
    if retry:
        rows.append([('♻️ 重新尝试', f'xs:{retry}:{_enc(sid)}')])
    rows.append([('🔙 返回测试服', f'xs:h:{_enc(sid)}')])
    return ikb(rows)


def _quota_lines(q, pool: str) -> str:
    if not q:
        return '· 名额 | 未初始化'
    pm_u, pm_t = xquota_pool_used(q, POOL_MAIN), xquota_pool_total(q, POOL_MAIN)
    po_u, po_t = xquota_pool_used(q, POOL_OPEN), xquota_pool_total(q, POOL_OPEN)
    mark = ' ←你的池'
    main_line = f"· 名额·主服用户 | {pm_u}/{pm_t}（剩 {max(0, pm_t - pm_u)}）"
    if pool == POOL_MAIN:
        main_line += mark
    open_line = f"· 名额·开放池(无主服号) | {po_u}/{po_t}（剩 {max(0, po_t - po_u)}）"
    if pool == POOL_OPEN:
        open_line += mark
    return f"{main_line}\n{open_line}"


# ---------------- 面板入口 & 主页 ----------------

@bot.on_callback_query(filters.regex('^xs:m') & user_in_group_on_filter)
async def xs_menu(_, call):
    """测试服入口：单服直达，多服列表"""
    servers = enabled_servers()
    if not servers:
        return await callAnswer(call, '⚠️ 管理员暂未开放测试服', True)
    if len(servers) == 1:
        return await xs_home_show(_, call, servers[0].id)
    rows = [[(f'🧪 {xc.name}', f'xs:h:{_enc(xc.id)}')] for xc in servers]
    rows.append([('🔙 返回', 'members')])
    await callAnswer(call, '🧪 请选择服务器')
    await editMessage(call, '**🧪 测试服 · 请选择服务器**', buttons=ikb(rows))


@bot.on_callback_query(filters.regex('^xs:h:') & user_in_group_on_filter)
async def xs_home_cb(_, call):
    return await xs_home_show(_, call, _dec(call.data.split(':')[2]))


async def xs_home_show(_, call, sid: str, answer: bool = True):
    xc = _xc_of(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 该服务器未开放', True)
    svc = get_service(sid)
    if svc is None:
        return await callAnswer(call, '⚠️ 服务器连接配置缺失', True)
    if answer:
        await callAnswer(call, f'🧪 {xc.name}')

    tg = call.from_user.id
    main = sql_get_emby(tg)
    a = xacc_get(sid, tg)
    q = xquota_get(sid)
    pool = _pool_of(main)
    kind, cost = _eligibility(xc, main, a)
    balance = int(getattr(main, 'iv', 0) or 0)

    acc_line = '· 未开通'
    password_line = ''
    if a and a.embyid:
        acc_line = f" · `{a.name}` | 状态 {a.lv} | 到期 `{a.ex}`"
        password_line = f"\n· 登录密码 | `{a.pwd}`"

    # 线路仅对已在本服开过号的用户展示
    line_block = f"· 线路 |\n{xc.line}" if (a and a.embyid) else "· 线路 | 🔒 开通账号后可见"
    offer_block = ''
    if not (a and a.embyid) and kind != 'none':
        offer_block = f"\n\n**你的本次开号方案**\n{_offer_text(xc, kind, cost, balance, a)}"
    text = (f"**▎🧪 {xc.name}面板**\n\n"
            f"· 我的账号{acc_line}{password_line}\n"
            f"{_quota_lines(q, pool)}\n"
            f"{line_block}\n\n"
            f"**开号通道**\n{_channel_text(xc, main)}{offer_block}\n\n"
            f"_用户名统一加前缀 `{xc.name_prefix}`_")

    buttons = []
    has_account = bool(a and a.embyid)
    slot_left = bool(q and xquota_pool_used(q, pool) < xquota_pool_total(q, pool))
    if has_account:
        buttons.append([('🗑️ 注销测试号', f'xs:d:{_enc(sid)}')])
        if a.lv != 'b':
            buttons.append([('⚠️ 账号已被管理员封印', f'xs:h:{_enc(sid)}')])
    elif kind != 'none':
        reopen_locked = (kind != 'grant' and not xc.allow_reopen
                         and xhist_has_opened(sid, tg))
        if reopen_locked:
            buttons.append([('🚫 你已体验过一次，名额留给别人', f'xs:h:{_enc(sid)}')])
        elif slot_left:
            if kind == 'points' and balance < cost:
                buttons.append([(f'🚫 {sakura_b}不足 · 需要 {cost}（持有 {balance}）',
                                 f'xs:h:{_enc(sid)}')])
            else:
                if main and main.embyid:
                    buttons.append([(f'🚀 一键开号{_fmt_offer(xc, kind, cost, a)}',
                                     f'xs:q:{_enc(sid)}')])
                buttons.append([(f'📝 单独开号{_fmt_offer(xc, kind, cost, a)}',
                                 f'xs:c:{_enc(sid)}')])
        else:
            buttons.append([('🚫 你所在名额池已满，稍后再试', f'xs:h:{_enc(sid)}')])
    grant_days = int(a.us or 0) if (a and not a.embyid) else 0
    if grant_days:
        buttons.append([(f'🎫 持有开号资格 {grant_days} 天', f'xs:h:{_enc(sid)}')])
    buttons.append([('🔙 返回', 'members')])
    await editMessage(call, text, buttons=ikb(buttons))


# ---------------- 开号公共逻辑 ----------------

async def _rollback(tg: int, sid: str, cost: int, paid_iv: bool,
                    embyid_created: str = None, restore_us: int = None,
                    pool: str = POOL_MAIN):
    """失败回滚链：远端半成品 → 积分/资格 → 名额；返回是否全部成功。"""
    ok = True
    if embyid_created:
        svc = get_service(sid)
        remote_ok = bool(svc and await svc.x_delete(embyid_created))
        ok = remote_ok and ok
    if paid_iv:
        main = sql_get_emby(tg)
        points_ok = bool(main and sql_update_emby(
            Emby.tg == tg, iv=int(main.iv or 0) + cost))
        ok = points_ok and ok
    if restore_us is not None:
        grant_ok = xacc_update(sid, tg, us=restore_us)
        ok = grant_ok and ok
    quota_ok = xquota_give(sid, pool)
    ok = quota_ok and ok
    if not ok:
        LOGGER.error(f'【xserver】开号回滚未完全成功 sid={sid} tg={tg} pool={pool} '
                     f'paid_iv={paid_iv} restore_us={restore_us is not None}')
    return ok


def _rollback_notice(kind: str, cost: int, days: int, rollback_ok: bool) -> str:
    if not rollback_ok:
        return ('\n\n⚠️ **自动回滚未完全成功，请立即联系管理员核对积分、资格和名额。**')
    if kind == 'points':
        return f'\n\n↩️ 已退回 `{cost}{sakura_b}` 并释放名额，余额已恢复到本次开号前。'
    if kind == 'grant':
        return f'\n\n↩️ 已恢复 `{days} 天` 开号资格并释放名额。'
    return '\n\n↩️ 名额已释放，本次未产生消费。'


def _write_account(sid: str, tg: int, pending, eid, name, pwd, pwd2, ex, pool: str) -> bool:
    """把资格待用行转正，或新建账号行；均记录占用的名额池，并记开号历史"""
    if pending is not None:
        ok = xacc_update(sid, tg, embyid=eid, name=name, pwd=pwd, pwd2=pwd2,
                         lv='b', cr=datetime.now(), ex=ex, us=0, pool=pool)
    else:
        ok = xacc_add(sid, tg, eid, name, pwd, pwd2, ex, pool=pool)
    if ok:
        xhist_mark(sid, tg)
    return ok


def _success_text(xc: Xserver, name, pwd, pwd2, ex, copied: bool,
                  kind: str, cost: int, balance_before: int, days: int) -> str:
    pwd_note = '（与主服一致）' if copied else '（请妥善保存）'
    if kind == 'points':
        settlement = (f'· 开通方式 | 积分兑换\n'
                      f'· {sakura_b}变动 | 已扣 `{cost}`，余额 `{balance_before} → {balance_before - cost}`')
    elif kind == 'grant':
        settlement = f'· 开通方式 | 已使用 `{days} 天` 开号资格'
    else:
        settlement = '· 开通方式 | 免费'
    return (f"**▎🧪 {xc.name}开号成功🎉**\n\n"
            f"· 用户名称 | `{name}`\n"
            f"· 用户密码 | `{pwd}`{pwd_note}\n"
            f"· 安全密码 | `{pwd2}`（仅发送一次）\n"
            f"· 本次有效期 | `{days} 天`\n"
            f"· 到期时间 | `{ex.strftime('%Y-%m-%d %H:%M:%S')}`（到期自动删除）\n"
            f"{settlement}\n\n"
            f"· 当前线路：\n{xc.line}")


async def _occupy_and_open(call, sid: str, want_name: str, want_pwd, want_pwd2: str,
                           copied: bool):
    """
    占名额（按主服状态选池）→ 判定通道/扣资源 → 建号（或收编远端残留同名号）→ 落库。
    调用方需已持有该用户的 user_lock。
    :param want_pwd: None 则随机生成
    :return: (ok, 成功文案或失败原因)
    """
    tg = call.from_user.id
    xc = _xc_of(sid)
    svc = get_service(sid)
    if xc is None or svc is None:
        return False, '⚠️ 该服务器未开放'

    main = sql_get_emby(tg)
    if main is None:
        return False, '⚠️ 数据库没有你，请重新 /start录入'
    a = xacc_get(sid, tg)
    if a and a.embyid:
        return False, '💦 你在该服务器已有账号，请勿重复开号'

    kind, cost = _eligibility(xc, main, a)
    if kind == 'none':
        return False, '🚫 当前未开放开号资格，请联系管理员或先获取注册码'
    balance_before = int(main.iv or 0)
    if kind == 'points' and balance_before < cost:
        return False, f'💦 {sakura_b}不足，开号需要 {cost}{sakura_b}，当前 {balance_before}'

    # 复开限制：默认一人一次（防脚本蹲坑回收后再抢）；grant 通道豁免，
    # 管理员直发资格即定向放行回锅用户
    if kind != 'grant' and not xc.allow_reopen and xhist_has_opened(sid, tg):
        return False, ('🚫 测试服账号**每人只限体验一次**，你的历史体验已结束，\n'
                       '名额请留给其他小伙伴。如需再次体验请联系管理员。')

    pool = _pool_of(main)
    pool_name = '主服用户' if pool == POOL_MAIN else '开放池'
    if not xquota_take(sid, pool):
        return False, f'**🚫 很抱歉，{xc.name}的{pool_name}名额已满。**'

    # 资格通道：有效期=资格天数并即时消耗；其他通道：服务器默认天数
    days = int(a.us or 0) if kind == 'grant' else xc.expire_days
    restore_us = int(a.us or 0) if kind == 'grant' else None

    paid_iv = False
    if kind == 'points':
        if not sql_update_emby(Emby.tg == tg, iv=balance_before - cost):
            quota_ok = xquota_give(sid, pool)
            if not quota_ok:
                LOGGER.error(f'【xserver】扣分失败后名额释放失败 sid={sid} tg={tg} pool={pool}')
            suffix = ('本次未扣除积分，名额已释放。' if quota_ok else
                      '本次未扣除积分，但名额释放异常，请联系管理员。')
            return False, f'❌ 扣分失败，请稍后重试。\n\n{suffix}'
        paid_iv = True
    if kind == 'grant':
        if not xacc_update(sid, tg, us=0):
            quota_ok = xquota_give(sid, pool)
            if not quota_ok:
                LOGGER.error(f'【xserver】资格锁定失败后名额释放失败 sid={sid} tg={tg} pool={pool}')
            suffix = ('资格未消耗，名额已释放。' if quota_ok else
                      '资格未消耗，但名额释放异常，请联系管理员。')
            return False, f'❌ 开号资格锁定失败，请稍后重试。\n\n{suffix}'

    if want_pwd is None:
        want_pwd = await pwd_create(8)

    # 名字被其他 bot 用户占用则拒绝（防收编他人账号）
    other = xacc_get_any(sid, want_name)
    if other is not None and other.tg != tg:
        rollback_ok = await _rollback(tg, sid, cost, paid_iv,
                                      restore_us=restore_us, pool=pool)
        return False, ('❌ 该用户名已被占用，请换一个' +
                       _rollback_notice(kind, cost, days, rollback_ok))

    ex = datetime.now() + timedelta(days=days)
    pending = a  # 可能为 None 或资格待用行

    # 远端同名残留（管理员手建 / 上次删除失败）→ 验主体验密后收编
    ok_u, uinfo = await svc.get_emby_user_by_name(want_name)
    if ok_u and isinstance(uinfo, dict) and uinfo.get("Id"):
        eid = str(uinfo["Id"])
        if not await svc.emby_reset(eid, want_pwd):
            rollback_ok = await _rollback(tg, sid, cost, paid_iv,
                                          restore_us=restore_us, pool=pool)
            return False, ('❌ 收编同名残留账号失败，请联系管理员' +
                           _rollback_notice(kind, cost, days, rollback_ok))
        await svc.emby_change_policy(eid, disable=False)
        if not _write_account(sid, tg, pending, eid, want_name, want_pwd, want_pwd2, ex, pool):
            rollback_ok = await _rollback(tg, sid, cost, paid_iv,
                                          restore_us=restore_us, pool=pool)
            return False, ('❌ 数据库写入失败，请稍后重试。' +
                           _rollback_notice(kind, cost, days, rollback_ok))
        LOGGER.info(f'【xserver】开号(收编/{pool}) {xc.name} {want_name} -> tg={tg} '
                    f'kind={kind} days={days} cost={cost if kind == "points" else 0}')
        await notify_xserver_opened(xc, tg)
        return True, _success_text(xc, want_name, want_pwd, want_pwd2, ex, copied=False,
                                   kind=kind, cost=cost, balance_before=balance_before,
                                   days=days)

    result = await svc.emby_create_x(name=want_name, days=days, password=want_pwd)
    if not result:
        rollback_ok = await _rollback(tg, sid, cost, paid_iv,
                                      restore_us=restore_us, pool=pool)
        return False, ('**- ❎ 已有此账户名，请重新输入\n'
                       '- ❔ 或检查用户名有无特殊字符\n'
                       f'- ❔ 或 {xc.name} 服务器连接不通，会话已结束！**' +
                       _rollback_notice(kind, cost, days, rollback_ok))
    eid, pwd, ex = result
    if not _write_account(sid, tg, pending, eid, want_name, pwd, want_pwd2, ex, pool):
        rollback_ok = await _rollback(tg, sid, cost, paid_iv, embyid_created=eid,
                                      restore_us=restore_us, pool=pool)
        return False, ('❌ 数据库写入失败，请稍后重试。' +
                       _rollback_notice(kind, cost, days, rollback_ok))
    LOGGER.info(f'【xserver】开号({pool}) {xc.name} {want_name} -> tg={tg} '
                f'kind={kind} days={days} cost={cost if kind == "points" else 0}')
    await notify_xserver_opened(xc, tg)
    return True, _success_text(xc, want_name, pwd, want_pwd2, ex, copied,
                               kind=kind, cost=cost, balance_before=balance_before,
                               days=days)


# ---------------- 一键开号 ----------------

@bot.on_callback_query(filters.regex('^xs:q:') & user_in_group_on_filter)
async def xs_quick(_, call):
    sid = _dec(call.data.split(':')[2])
    xc = _xc_of(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 该服务器未开放', True)
    tg = call.from_user.id
    main = sql_get_emby(tg)
    if not main or not main.embyid:
        return await callAnswer(call, '💦 你在主服还没有账号，请走【单独开号】', True)
    if main.lv not in ('a', 'b'):
        return await callAnswer(call, '🚫 主服账号状态异常（未激活/已封禁），无法一键开号', True)
    a = xacc_get(sid, tg)
    if a and a.embyid:
        return await callAnswer(call, '💦 你在该服务器已有账号，请勿重复开号', True)
    kind, cost = _eligibility(xc, main, a)
    balance = int(main.iv or 0)
    if kind == 'none':
        return await callAnswer(call, '🚫 当前未开放开号资格', True)
    if kind == 'points' and balance < cost:
        return await callAnswer(call, f'💦 {sakura_b}不足，需要 {cost}，当前 {balance}', True)
    if kind != 'grant' and not xc.allow_reopen and xhist_has_opened(sid, tg):
        return await callAnswer(call, '🚫 你已体验过一次，无法自行复开', True)

    want_name = f"{xc.name_prefix}{main.name}"
    await callAnswer(call, '请确认本次开号信息')
    await editMessage(
        call,
        f'**🧪 确认开通 {xc.name}账号**\n\n'
        f'· 最终用户名 | `{want_name}`\n'
        f'{_offer_text(xc, kind, cost, balance, a)}\n\n'
        f'_确认后开始创建；执行失败会自动退回积分、资格和名额。_',
        buttons=ikb([[('✅ 确认开号', f'xs:qc:{_enc(sid)}')],
                     [('🔙 取消', f'xs:h:{_enc(sid)}')]]),
    )


@bot.on_callback_query(filters.regex('^xs:qc:') & user_in_group_on_filter)
async def xs_quick_confirm(_, call):
    sid = _dec(call.data.split(':')[2])
    xc = _xc_of(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 该服务器未开放', True)
    await callAnswer(call, '⏳ 正在创建账号')
    tg = call.from_user.id
    async with get_user_lock(tg):
        main = sql_get_emby(tg)
        if not main or not main.embyid:
            ok, out = False, '💦 你在主服还没有账号，请走【单独开号】'
        elif main.lv not in ('a', 'b'):
            ok, out = False, '🚫 主服账号状态异常（未激活/已封禁），无法一键开号'
        else:
            want_name = f"{xc.name_prefix}{main.name}"
            await editMessage(call, f'**⏳ 正在创建 {xc.name}账号**\n\n'
                                    f'· 最终用户名 | `{want_name}`\n'
                                    f'· 当前状态 | 正在初始化账号与权限，请稍候...')
            want_pwd = main.pwd if main.pwd else None
            want_pwd2 = main.pwd2 if main.pwd2 else await pwd_create(4)
            ok, out = await _occupy_and_open(call, sid, want_name, want_pwd, want_pwd2,
                                             copied=bool(main.pwd))
    await editMessage(call, out, buttons=_result_buttons(sid, None if ok else 'q'))


# ---------------- 单独开号 ----------------

@bot.on_callback_query(filters.regex('^xs:c:') & user_in_group_on_filter)
async def xs_create(_, call):
    sid = _dec(call.data.split(':')[2])
    xc = _xc_of(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 该服务器未开放', True)
    main = sql_get_emby(call.from_user.id)
    a = xacc_get(sid, call.from_user.id)
    if main is None:
        return await callAnswer(call, '⚠️ 数据库没有你，请重新 /start录入', True)
    if a and a.embyid:
        return await callAnswer(call, '💦 你在该服务器已有账号，请勿重复开号', True)
    kind, cost = _eligibility(xc, main, a)
    balance = int(getattr(main, 'iv', 0) or 0)
    if kind == 'none':
        return await callAnswer(call, '🚫 当前未开放开号资格', True)
    if kind == 'points' and balance < cost:
        return await callAnswer(
            call, f'💦 {sakura_b}不足，开号需要 {cost}{sakura_b}，当前 {balance}', True)
    days = _offer_days(xc, kind, a)
    retry_buttons = _result_buttons(sid, 'c')
    await callAnswer(call, f'🧪 {xc.name}单独开号')
    msg = await ask_return(call,
                           text=f'🧪 **{xc.name} 开号**:\n\n'
                                f'{_offer_text(xc, kind, cost, balance, a)}\n\n'
                                '• 请在2min内输入 `[用户名][空格][安全码]`\n'
                                f'• 服务器上的最终用户名为 `{xc.name_prefix}用户名`\n'
                                '• 举个例子🌰：`苏苏 1234`\n\n'
                                '• 用户名限中/英文，🚫**特殊字符与空格**\n'
                                '• 发送信息即确认按以上方案开号\n'
                                '• 退出请点 /cancel', timer=120, button=retry_buttons)
    if not msg:
        return
    if msg.text.strip() == '/cancel':
        await msg.delete()
        return await xs_home_show(_, call, sid, answer=False)
    try:
        raw_name, pwd2 = msg.text.split()
    except ValueError:
        return await msg.reply(f'⚠️ 输入格式错误\n\n`{msg.text}`\n **会话已结束！**',
                               reply_markup=retry_buttons)
    reject_reason = _username_reject_reason(raw_name, xc.name_prefix)
    if reject_reason:
        LOGGER.warning(f'【xserver】拒绝单独开号 sid={sid} tg={call.from_user.id}: {reject_reason}')
        return await msg.reply(f'⚠️ {reject_reason}，会话已结束！',
                               reply_markup=retry_buttons)
    want_name = f"{xc.name_prefix}{raw_name}"
    status = await msg.reply(f'**⏳ 已收到开号信息**\n\n'
                             f'· 最终用户名 | `{want_name}`\n'
                             f'· 当前状态 | 正在初始化账号与权限，请稍候...')
    async with get_user_lock(call.from_user.id):
        current_main = sql_get_emby(call.from_user.id)
        current_a = xacc_get(sid, call.from_user.id)
        current_kind, current_cost = _eligibility(xc, current_main, current_a)
        current_days = _offer_days(xc, current_kind, current_a)
        if (current_kind, current_cost, current_days) != (kind, cost, days):
            ok = False
            out = ('⚠️ 开号条件在输入期间发生变化，本次未扣除积分或资格。\n\n'
                   '请返回测试服面板重新确认。')
        else:
            ok, out = await _occupy_and_open(call, sid, want_name, None, pwd2, copied=False)
    await editMessage(status, out, buttons=_result_buttons(sid, None if ok else 'c'))


# ---------------- 注销 ----------------

@bot.on_callback_query(filters.regex('^xs:d:') & user_in_group_on_filter)
async def xs_del(_, call):
    sid = _dec(call.data.split(':')[2])
    a = xacc_get(sid, call.from_user.id)
    if not a or not a.embyid:
        return await callAnswer(call, '💦 你在该服务器没有账号', True)
    await callAnswer(call, '⚠️ 注销将立即删除账号并回收名额', True)
    await editMessage(call, f'**⚠️ 确认注销 `{a.name}` ？**\n\n账号将被立即删除，名额归还。',
                      buttons=ikb([[('🎯 确认注销', f'xs:dc:{_enc(sid)}')],
                                   [('🔙 取消', f'xs:h:{_enc(sid)}')]]))


@bot.on_callback_query(filters.regex('^xs:dc:') & user_in_group_on_filter)
async def xs_del_confirm(_, call):
    sid = _dec(call.data.split(':')[2])
    tg = call.from_user.id
    async with get_user_lock(tg):
        a = xacc_get(sid, tg)
        if not a or not a.embyid:
            return await callAnswer(call, '💦 账号已不存在', True)
        svc = get_service(sid)
        if svc is None:
            return await callAnswer(call, '⚠️ 服务器未开放，无法注销', True)
        await callAnswer(call, '🗑️ 正在注销...')
        deleted = await svc.x_delete(a.embyid)
        if not deleted:
            # 远端已不存在（404）视为删除成功，其余失败中止
            ok_u, uinfo = await svc.user(a.embyid)
            gone = (not ok_u) and isinstance(uinfo, dict) and '资源不存在' in uinfo.get('error', '')
            if not gone:
                return await editMessage(call, '❌ 注销失败：无法连接服务器，请稍后重试或联系管理员。',
                                         buttons=ikb([[('🔙 返回', f'xs:h:{_enc(sid)}')]]))
        xacc_delete(sid, tg)
        xquota_give(sid, a.pool or POOL_MAIN)
        name = a.name
        xc = _xc_of(sid)
        if xc is not None:
            await notify_xserver_deleted(xc, tg, '用户主动注销')
    await editMessage(call, f"**✅ 已注销 `{name}`，名额已回收。**",
                      buttons=ikb([[('🔙 返回', f'xs:h:{_enc(sid)}')]]))
    LOGGER.info(f'【xserver】注销 {sid} {name} -> tg={tg}')
