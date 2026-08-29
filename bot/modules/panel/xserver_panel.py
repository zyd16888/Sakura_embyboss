"""
xserver（测试服等旁路扩展服务器）用户面板。

与主服 member_panel / emby 表完全隔离：
- 账号只读写 xserver_account / xserver_quota / xserver_code；
- 唯一触碰主服数据的地方是「积分通道扣 emby.iv」（复用现有字段，不改结构）。

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
from bot.func_helper.xserver import get_service, enabled_servers
from bot.schemas import Xserver
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby
from bot.sql_helper.sql_xserver import (xacc_get, xacc_get_any, xacc_add, xacc_update,
                                        xacc_delete, xquota_get, xquota_take, xquota_give)

# 用户名禁用字符：空白 + markdown/Emby 敏感符号
_BAD_CHARS = re.compile(r'[\s~`*_\[\]()#+\\|<>:/?@&%$^{}=";,\']')
_NAME_MAX = 48


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


async def xs_home_show(_, call, sid: str):
    xc = _xc_of(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 该服务器未开放', True)
    svc = get_service(sid)
    if svc is None:
        return await callAnswer(call, '⚠️ 服务器连接配置缺失', True)
    await callAnswer(call, f'🧪 {xc.name}')

    tg = call.from_user.id
    main = sql_get_emby(tg)
    a = xacc_get(sid, tg)
    q = xquota_get(sid)

    acc_line = '· 未开通'
    if a and a.embyid:
        acc_line = f" · `{a.name}` | 状态 {a.lv} | 到期 `{a.ex}`"
    quota_line = (f"· 名额 | {q.used}/{q.total}（剩余 {max(0, q.total - q.used)}）"
                  if q else '· 名额 | 未初始化')

    text = (f"**▎🧪 {xc.name}面板**\n\n"
            f"· 我的账号{acc_line}\n"
            f"{quota_line}\n"
            f"· 线路 |\n{xc.line}\n\n"
            f"**开号通道**\n{_channel_text(xc, main)}\n\n"
            f"_用户名统一加前缀 `{xc.name_prefix}`；到期自动删除并回收名额_")

    buttons = []
    kind, cost = _eligibility(xc, main, a)
    has_account = bool(a and a.embyid)
    slot_left = bool(q and q.used < q.total)
    if has_account:
        buttons.append([('🗑️ 注销测试号', f'xs:d:{_enc(sid)}')])
        if a.lv != 'b':
            buttons.append([('⚠️ 账号已被管理员封印', f'xs:h:{_enc(sid)}')])
    elif kind != 'none':
        if slot_left:
            if main and main.embyid:
                buttons.append([(f'🚀 一键开号{_fmt_cost(kind, cost)}', f'xs:q:{_enc(sid)}')])
            buttons.append([(f'📝 单独开号{_fmt_cost(kind, cost)}', f'xs:c:{_enc(sid)}')])
        else:
            buttons.append([('🚫 名额已满，稍后再试', f'xs:h:{_enc(sid)}')])
    grant_days = int(a.us or 0) if (a and not a.embyid) else 0
    if grant_days:
        buttons.append([(f'🎫 持有开号资格 {grant_days} 天', f'xs:h:{_enc(sid)}')])
    buttons.append([('🔙 返回', 'members')])
    await editMessage(call, text, buttons=ikb(buttons))


# ---------------- 开号公共逻辑 ----------------

async def _rollback(tg: int, sid: str, cost: int, paid_iv: bool,
                    embyid_created: str = None, restore_us: int = None):
    """失败回滚链：远端半成品 → 积分/资格 → 名额"""
    if embyid_created:
        svc = get_service(sid)
        if svc:
            await svc.x_delete(embyid_created)
    if paid_iv:
        main = sql_get_emby(tg)
        if main:
            sql_update_emby(Emby.tg == tg, iv=int(main.iv or 0) + cost)
    if restore_us is not None:
        xacc_update(sid, tg, us=restore_us)
    xquota_give(sid)


def _write_account(sid: str, tg: int, pending, eid, name, pwd, pwd2, ex) -> bool:
    """把资格待用行转正，或新建账号行"""
    if pending is not None:
        return xacc_update(sid, tg, embyid=eid, name=name, pwd=pwd, pwd2=pwd2,
                           lv='b', cr=datetime.now(), ex=ex, us=0)
    return xacc_add(sid, tg, eid, name, pwd, pwd2, ex)


def _success_text(xc: Xserver, name, pwd, pwd2, ex, copied: bool) -> str:
    pwd_note = '（与主服一致）' if copied else '（请妥善保存）'
    return (f"**▎🧪 {xc.name}开号成功🎉**\n\n"
            f"· 用户名称 | `{name}`\n"
            f"· 用户密码 | `{pwd}`{pwd_note}\n"
            f"· 安全密码 | `{pwd2}`（仅发送一次）\n"
            f"· 到期时间 | `{ex.strftime('%Y-%m-%d %H:%M:%S')}`（到期自动删除）\n\n"
            f"· 当前线路：\n{xc.line}")


async def _occupy_and_open(call, sid: str, want_name: str, want_pwd, want_pwd2: str,
                           copied: bool):
    """
    占名额 → 判定通道/扣资源 → 建号（或收编远端残留同名号）→ 落库。
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
    if kind == 'points' and int(main.iv or 0) < cost:
        return False, f'💦 {sakura_b}不足，开号需要 {cost}{sakura_b}，当前 {int(main.iv or 0)}'

    if not xquota_take(sid):
        return False, f'**🚫 很抱歉，{xc.name}名额已满。**'

    # 资格通道：有效期=资格天数并即时消耗；其他通道：服务器默认天数
    days = int(a.us or 0) if kind == 'grant' else xc.expire_days
    restore_us = int(a.us or 0) if kind == 'grant' else None

    paid_iv = False
    if kind == 'points':
        if not sql_update_emby(Emby.tg == tg, iv=int(main.iv or 0) - cost):
            xquota_give(sid)
            return False, '❌ 扣分失败，请稍后重试。'
        paid_iv = True
    if kind == 'grant':
        xacc_update(sid, tg, us=0)

    if want_pwd is None:
        want_pwd = await pwd_create(8)

    # 名字被其他 bot 用户占用则拒绝（防收编他人账号）
    other = xacc_get_any(sid, want_name)
    if other is not None and other.tg != tg:
        await _rollback(tg, sid, cost, paid_iv, restore_us=restore_us)
        return False, '❌ 该用户名已被占用，请换一个'

    ex = datetime.now() + timedelta(days=days)
    pending = a  # 可能为 None 或资格待用行

    # 远端同名残留（管理员手建 / 上次删除失败）→ 验主体验密后收编
    ok_u, uinfo = await svc.get_emby_user_by_name(want_name)
    if ok_u and isinstance(uinfo, dict) and uinfo.get("Id"):
        eid = str(uinfo["Id"])
        if not await svc.emby_reset(eid, want_pwd):
            await _rollback(tg, sid, cost, paid_iv, restore_us=restore_us)
            return False, '❌ 收编同名残留账号失败，请联系管理员'
        await svc.emby_change_policy(eid, disable=False)
        if not _write_account(sid, tg, pending, eid, want_name, want_pwd, want_pwd2, ex):
            await _rollback(tg, sid, cost, paid_iv, restore_us=restore_us)
            return False, '❌ 数据库写入失败，请稍后重试。'
        LOGGER.info(f'【xserver】开号(收编) {xc.name} {want_name} -> tg={tg}')
        return True, _success_text(xc, want_name, want_pwd, want_pwd2, ex, copied=False)

    result = await svc.emby_create_x(name=want_name, days=days, password=want_pwd)
    if not result:
        await _rollback(tg, sid, cost, paid_iv, restore_us=restore_us)
        return False, ('**- ❎ 已有此账户名，请重新输入\n'
                       '- ❔ 或检查用户名有无特殊字符\n'
                       f'- ❔ 或 {xc.name} 服务器连接不通，会话已结束！**')
    eid, pwd, ex = result
    if not _write_account(sid, tg, pending, eid, want_name, pwd, want_pwd2, ex):
        await _rollback(tg, sid, cost, paid_iv, embyid_created=eid, restore_us=restore_us)
        return False, '❌ 数据库写入失败，请稍后重试。'
    LOGGER.info(f'【xserver】开号 {xc.name} {want_name} -> tg={tg}')
    return True, _success_text(xc, want_name, pwd, want_pwd2, ex, copied)


# ---------------- 一键开号 ----------------

@bot.on_callback_query(filters.regex('^xs:q:') & user_in_group_on_filter)
async def xs_quick(_, call):
    sid = _dec(call.data.split(':')[2])
    xc = _xc_of(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 该服务器未开放', True)
    async with get_user_lock(call.from_user.id):
        main = sql_get_emby(call.from_user.id)
        if not main or not main.embyid:
            return await callAnswer(call, '💦 你在主服还没有账号，请走【单独开号】', True)
        if main.lv not in ('a', 'b'):
            return await callAnswer(call, '🚫 主服账号状态异常（未激活/已封禁），无法一键开号', True)
        want_name = f"{xc.name_prefix}{main.name}"
        want_pwd = main.pwd if main.pwd else None
        want_pwd2 = main.pwd2 if main.pwd2 else await pwd_create(4)
        ok, out = await _occupy_and_open(call, sid, want_name, want_pwd, want_pwd2,
                                         copied=bool(main.pwd))
    await callAnswer(call, '✅ 完成' if ok else '⚠️ 未成功', not ok)
    await sendMessage(call, out, buttons=ikb([[('🔙 返回', f'xs:h:{_enc(sid)}')]]))


# ---------------- 单独开号 ----------------

@bot.on_callback_query(filters.regex('^xs:c:') & user_in_group_on_filter)
async def xs_create(_, call):
    sid = _dec(call.data.split(':')[2])
    xc = _xc_of(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 该服务器未开放', True)
    msg = await ask_return(call,
                           text=f'🧪 **{xc.name} 开号**:\n\n'
                                '• 请在2min内输入 `[用户名][空格][安全码]`\n'
                                f'• 服务器上的最终用户名为 `{xc.name_prefix}用户名`\n'
                                '• 举个例子🌰：`苏苏 1234`\n\n'
                                '• 用户名限中/英文，🚫**特殊字符与空格**\n'
                                '• 退出请点 /cancel', timer=120)
    if not msg:
        return
    if msg.text.strip() == '/cancel':
        return await msg.delete()
    try:
        raw_name, pwd2 = msg.text.split()
    except ValueError:
        return await msg.reply(f'⚠️ 输入格式错误\n\n`{msg.text}`\n **会话已结束！**')
    want_name = f"{xc.name_prefix}{raw_name}"
    if len(want_name) > _NAME_MAX or _BAD_CHARS.search(want_name):
        return await msg.reply('⚠️ 用户名含空格/特殊字符或过长，会话已结束！')
    async with get_user_lock(call.from_user.id):
        ok, out = await _occupy_and_open(call, sid, want_name, None, pwd2, copied=False)
    await sendMessage(msg, out, buttons=ikb([[('🔙 返回', f'xs:h:{_enc(sid)}')]]))


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
        xc = _xc_of(sid)
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
        xquota_give(sid)
        name = a.name
    await editMessage(call, f"**✅ 已注销 `{name}`，名额已回收。**",
                      buttons=ikb([[('🔙 返回', f'xs:h:{_enc(sid)}')]]))
    LOGGER.info(f'【xserver】注销 {sid} {name} -> tg={tg}')
