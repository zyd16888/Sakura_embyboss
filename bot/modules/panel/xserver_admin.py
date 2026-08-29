"""
xserver（测试服）管理面板 —— 全部走新表新文件，不触碰主服管理逻辑。

入口：
- 私聊 `!xspanel`                    管理主页（名额/通道开关/开码/账号列表）
- `!xsin  <tg|用户名> [server_id]`   查询账号
- `!xsgr  <tg> <天数> [server_id]`   直接发放开号资格
- `!xsext <用户名|tg> <天数> [server_id]`  续期（封印号顺延即解封）
- `!xsrm  <用户名|tg> [server_id]`   删除账号并回收名额
- `!xscrn <数量> <天数> [server_id]` 开续期码（资格码走面板按钮）

回调前缀 `xsa:`，server_id 一律 hex 编码，避免与其他 handler 正则冲突。
"""
from datetime import datetime, timedelta

from pyrogram import filters
from pyrogram.types import CallbackQuery
from pyromod.helpers import ikb

from bot import bot, prefixes, sakura_b, config, ranks, LOGGER, save_config
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.msg_utils import (callAnswer, editMessage, sendMessage,
                                       ask_return, deleteMessage)
from bot.func_helper.utils import pwd_create, split_long_message
from bot.func_helper.xserver import get_service, enabled_servers
from bot.sql_helper.sql_emby import sql_get_emby
from bot.sql_helper.sql_xserver import (xacc_get, xacc_get_any, xacc_all, xacc_update,
                                        xacc_delete, xacc_grant, xquota_get,
                                        xquota_set_total, xquota_give, xcode_add)
from bot.modules.panel.xserver_panel import _enc, _dec, _xc_of


# ---------------- 参数解析 ----------------

def _default_sid() -> str:
    servers = enabled_servers()
    return servers[0].id if servers else ''


def _known_sid(arg) -> bool:
    return any(xc.id == arg for xc in (config.xservers or []))


def _resolve_sid(arg=None) -> str:
    """显式 server_id 优先；未知 id 回落到首个启用服"""
    if arg and _known_sid(arg):
        return arg
    return _default_sid()


def _parse_sid_tail(command):
    """`<主参数...> [server_id]`：末位是非数字且已配置的 server_id 则剥出"""
    args = list(command[1:])
    sid = None
    if args and not args[-1].lstrip('-').isdigit() and _known_sid(args[-1]):
        sid = args.pop()
    return args, _resolve_sid(sid)


def _to_key(raw: str):
    return int(raw) if raw.lstrip('-').isdigit() else raw


# ---------------- 面板主页 ----------------

def _admin_home_text(xc, q) -> str:
    ch = xc.channels
    on = lambda b: '✅' if b else '❎'  # noqa: E731
    quota_line = (f"· 名额 | {q.used}/{q.total}（剩 {max(0, q.total - q.used)}）"
                  if q else "· 名额 | 未初始化")
    return (f"**▎🧪 {xc.name} · 管理**\n\n"
            f"· 地址 | `{xc.url}`\n"
            f"· 线路 | {xc.line}\n"
            f"{quota_line}\n"
            f"· 有效期 | {xc.expire_days} 天 · 前缀 `{xc.name_prefix}` · 并发 {xc.limit}\n\n"
            f"**开号通道**\n"
            f"{on(ch.main_user)} 主服一键 · {on(ch.whitelist)} 白名单免费 · "
            f"{on(ch.points)} 积分 {ch.points_cost}{sakura_b}")


def _admin_home_ikb(sid: str, xc) -> list:
    ch = xc.channels
    dot = lambda b: '🟢' if b else '🔴'  # noqa: E731
    return [
        [('➖ 减名额', f'xsa:qm:{_enc(sid)}'), ('➕ 加名额', f'xsa:qp:{_enc(sid)}'),
         ('🎚 设总数', f'xsa:qt:{_enc(sid)}')],
        [(f'{dot(ch.main_user)}主服一键', f'xsa:tc:{_enc(sid)}:main_user'),
         (f'{dot(ch.whitelist)}白名单', f'xsa:tc:{_enc(sid)}:whitelist'),
         (f'{dot(ch.points)}积分兑换', f'xsa:tc:{_enc(sid)}:points')],
        [(f'💰 改单价{ch.points_cost}', f'xsa:pc:{_enc(sid)}')],
        [('🎫 开资格码', f'xsa:cr:{_enc(sid)}'), ('📋 账号列表', f'xsa:li:{_enc(sid)}:0')],
        [('🔙 返回', 'manage')],
    ]


async def _admin_home_render(_, update, sid: str):
    xc = _xc_of(sid)
    if xc is None:
        return await callAnswer(update, '⚠️ 该服务器未开放', True)
    get_service(sid)  # 确保 quota 种子
    q = xquota_get(sid)
    if isinstance(update, CallbackQuery):
        await callAnswer(update, f'🧪 {xc.name}')
    return await editMessage(update, _admin_home_text(xc, q),
                             buttons=ikb(_admin_home_ikb(sid, xc)))


@bot.on_message(filters.command('xspanel', prefixes) & admins_on_filter & filters.private)
async def xs_admin_cmd(_, msg):
    await deleteMessage(msg)
    servers = enabled_servers()
    if not servers:
        return await sendMessage(msg, '⚠️ 尚未配置启用的测试服（config.xservers）', timer=60)
    if len(servers) == 1:
        return await _admin_home_render(_, msg, servers[0].id)
    rows = [[(f'🧪 {xc.name}', f'xsa:h:{_enc(xc.id)}')] for xc in servers]
    await sendMessage(msg, '**🧪 测试服管理 · 选择服务器**', buttons=ikb(rows))


@bot.on_callback_query(filters.regex('^xsa:m$') & admins_on_filter)
async def xs_admin_menu_cb(_, call):
    servers = enabled_servers()
    if not servers:
        return await callAnswer(call, '⚠️ 无启用的测试服', True)
    if len(servers) == 1:
        return await _admin_home_render(_, call, servers[0].id)
    rows = [[(f'🧪 {xc.name}', f'xsa:h:{_enc(xc.id)}')] for xc in servers]
    await editMessage(call, '**🧪 测试服管理 · 选择服务器**', buttons=ikb(rows))


@bot.on_callback_query(filters.regex('^xsa:h:') & admins_on_filter)
async def xs_admin_home_cb(_, call):
    return await _admin_home_render(_, call, _dec(call.data.split(':')[2]))


# ---------------- 名额调整 ----------------

@bot.on_callback_query(filters.regex('^xsa:q[pm]:') & admins_on_filter)
async def xs_quota_add_minus(_, call):
    act, sid_hex = call.data.split(':')[1], call.data.split(':')[2]
    sid = _dec(sid_hex)
    delta = 1 if act == 'qp' else -1
    q = xquota_get(sid)
    if q is None:
        return await callAnswer(call, '⚠️ 名额未初始化', True)
    new_total = int(q.total or 0) + delta
    if new_total < int(q.used or 0):
        return await callAnswer(call, f'⚠️ 总名额不得低于已用 {q.used}', True)
    xquota_set_total(sid, new_total)
    await _admin_home_render(_, call, sid)


@bot.on_callback_query(filters.regex('^xsa:qt:') & admins_on_filter)
async def xs_quota_set(_, call):
    sid = _dec(call.data.split(':')[2])
    msg = await ask_return(call, text='🎚 发送新的**总名额**数字（取消 /cancel）', timer=120)
    if not msg:
        return
    if msg.text.strip() == '/cancel':
        return await _admin_home_render(_, call, sid)
    try:
        total = int(msg.text.strip())
        assert total >= 0
    except (ValueError, AssertionError):
        return await sendMessage(call, '⚠️ 请发送非负整数', timer=60)
    q = xquota_get(sid)
    if q and total < int(q.used or 0):
        return await sendMessage(call, f'⚠️ 当前已用 {q.used}，总名额不得低于此数', timer=60)
    xquota_set_total(sid, total)
    await _admin_home_render(_, call, sid)


# ---------------- 通道开关 & 单价 ----------------

@bot.on_callback_query(filters.regex('^xsa:tc:') & admins_on_filter)
async def xs_toggle_channel(_, call):
    _, _, sid_hex, chan = call.data.split(':')
    sid = _dec(sid_hex)
    xc = _xc_of(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 服务器不存在', True)
    ch = xc.channels
    setattr(ch, chan, not getattr(ch, chan))
    save_config()
    LOGGER.info(f'【xserver管理】{call.from_user.id} 切换 {sid} 通道 {chan} -> '
                f'{getattr(ch, chan)}')
    await _admin_home_render(_, call, sid)


@bot.on_callback_query(filters.regex('^xsa:pc:') & admins_on_filter)
async def xs_points_cost(_, call):
    sid = _dec(call.data.split(':')[2])
    msg = await ask_return(call, text=f'💰 发送新的积分开号单价（{sakura_b}，取消 /cancel）', timer=120)
    if not msg:
        return
    if msg.text.strip() == '/cancel':
        return await _admin_home_render(_, call, sid)
    try:
        cost = int(msg.text.strip())
        assert cost > 0
    except (ValueError, AssertionError):
        return await sendMessage(call, '⚠️ 请发送正整数', timer=60)
    xc = _xc_of(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 服务器不存在', True)
    xc.channels.points_cost = cost
    save_config()
    LOGGER.info(f'【xserver管理】{call.from_user.id} 设置 {sid} 积分单价 -> {cost}')
    await _admin_home_render(_, call, sid)


# ---------------- 开注册码 ----------------

async def _emit_codes(msg, sid: str, count: int, days: int, kind: str):
    mark = '-XREG_' if kind == 'reg' else '-XRNV_'
    codes = [f'{ranks.logo}-{sid}{mark}{await pwd_create(10)}' for _ in range(count)]
    if not xcode_add(codes, sid, msg.from_user.id, days, kind):
        return await msg.reply('❌ 注册码写入数据库失败，请重试')
    head = (f'🧪 `{sid}` {"开号资格" if kind == "reg" else "续期"}码 · '
            f'{count} 张 × {days} 天\n\n')
    for chunk in split_long_message(head + '\n'.join(codes), max_length=1800):
        await msg.reply(chunk)
    LOGGER.info(f'【xserver管理】{msg.from_user.id} 在 {sid} 开出 {kind} 码 {count}张/{days}天')
    return True


@bot.on_callback_query(filters.regex('^xsa:cr:') & admins_on_filter)
async def xs_create_code_ask(_, call):
    sid = _dec(call.data.split(':')[2])
    if _xc_of(sid) is None:
        return await callAnswer(call, '⚠️ 服务器不存在', True)
    msg = await ask_return(call,
                           text='🎫 **【开测试服资格码】**\n\n'
                                '请在2min内发送 `[数量][空格][天数]`，例：`3 30`\n'
                                '· 用户兑换后获得 N 天开号资格（开号时有效期=资格天数）\n'
                                '· 续期码请用命令 `/xscrn <数量> <天数> [server_id]`\n'
                                '退出 /cancel', timer=120)
    if not msg:
        return
    if msg.text.strip() == '/cancel':
        return await _admin_home_render(_, call, sid)
    try:
        count_s, days_s = msg.text.split()
        count, days = int(count_s), int(days_s)
        assert 1 <= count <= 50 and 1 <= days <= 3650
    except (ValueError, AssertionError):
        return await msg.reply('⚠️ 格式错误，正确如 `3 30`（数量1-50，天数1-3650）')
    await _emit_codes(msg, sid, count, days, 'reg')


@bot.on_message(filters.command('xscrn', prefixes) & admins_on_filter & filters.private)
async def xs_cmd_renew_code(_, msg):
    await deleteMessage(msg)
    args, sid = _parse_sid_tail(msg.command)
    if len(args) != 2:
        return await sendMessage(msg, '用法：`/xscrn <数量> <天数> [server_id]`', timer=60)
    try:
        count, days = int(args[0]), int(args[1])
        assert 1 <= count <= 50 and 1 <= days <= 3650
    except (ValueError, AssertionError):
        return await sendMessage(msg, '⚠️ 数量1-50、天数1-3650', timer=60)
    if _xc_of(sid) is None:
        return await sendMessage(msg, '⚠️ 无启用的测试服', timer=60)
    await _emit_codes(msg, sid, count, days, 'renew')


# ---------------- 管理命令：查询/发资格/续期/删除 ----------------

@bot.on_message(filters.command('xsin', prefixes) & admins_on_filter)
async def xs_admin_info(_, msg):
    await deleteMessage(msg)
    args, sid = _parse_sid_tail(msg.command)
    if len(args) != 1:
        return await sendMessage(msg, '用法：`/xsin <tg|用户名> [server_id]`', timer=60)
    if _xc_of(sid) is None:
        return await sendMessage(msg, '⚠️ 无启用的测试服', timer=60)
    a = xacc_get_any(sid, _to_key(args[0]))
    if a is None:
        return await sendMessage(msg, f'❌ `{sid}` 未找到账号 `{args[0]}`', timer=60)
    xc = _xc_of(sid)
    q = xquota_get(sid)
    quota_line = f"· 当前名额 | {q.used}/{q.total}" if q else ''
    text = (f"**▎🧪 {xc.name} 账号档案**\n\n"
            f"· TG | `{a.tg}`\n"
            f"· EmbyID | `{a.embyid or '（未开号，持资格）'}`\n"
            f"· 用户名 | `{a.name or '-'}`\n"
            f"· 状态 | `{a.lv}` · 资格 {int(a.us or 0)} 天\n"
            f"· 创建 | {a.cr} · 到期 | {a.ex}\n"
            f"{quota_line}")
    await sendMessage(msg, text, buttons=ikb([[('🔙 主页', 'xsa:m')]]))


@bot.on_message(filters.command('xsgr', prefixes) & admins_on_filter & filters.private)
async def xs_admin_grant(_, msg):
    await deleteMessage(msg)
    args, sid = _parse_sid_tail(msg.command)
    if len(args) != 2:
        return await sendMessage(msg, '用法：`/xsgr <tg> <天数> [server_id]`', timer=60)
    try:
        tg, days = int(args[0]), int(args[1])
        assert days > 0
    except (ValueError, AssertionError):
        return await sendMessage(msg, '⚠️ tg/天数须为正整数', timer=60)
    if _xc_of(sid) is None:
        return await sendMessage(msg, f'⚠️ 测试服 `{sid}` 未启用', timer=60)
    if sql_get_emby(tg) is None and xacc_get(sid, tg) is None:
        return await sendMessage(msg, '⚠️ 该 TG 未 /start 过 bot，无法发放资格', timer=60)
    xacc_grant(sid, tg, days)
    LOGGER.info(f'【xserver管理】{msg.from_user.id} 给 {tg} 在 {sid} 发放资格 {days} 天')
    await sendMessage(msg, f'✅ 已给 `{tg}` 在 `{sid}` 发放开号资格 **{days}** 天')
    try:
        await bot.send_message(tg, f'🎉 管理员发放了测试服 `{sid}` 开号资格 {days} 天，'
                                   f'前往【🧪 测试服开号】使用')
    except Exception as e:
        LOGGER.warning(f'【xserver管理】资格通知失败 {tg}: {e}')


@bot.on_message(filters.command('xsext', prefixes) & admins_on_filter & filters.private)
async def xs_admin_extend(_, msg):
    await deleteMessage(msg)
    args, sid = _parse_sid_tail(msg.command)
    if len(args) != 2:
        return await sendMessage(msg, '用法：`/xsext <用户名|tg> <天数> [server_id]`', timer=60)
    try:
        days = int(args[1])
        assert days != 0
    except (ValueError, AssertionError):
        return await sendMessage(msg, '⚠️ 天数须为非零整数', timer=60)
    a = xacc_get_any(sid, _to_key(args[0]))
    if a is None or not a.embyid:
        return await sendMessage(msg, f'❌ `{sid}` 未找到已开通账号 `{args[0]}`', timer=60)
    base = a.ex if (a.ex and a.ex > datetime.now()) else datetime.now()
    new_ex = base + timedelta(days=days)
    xacc_update(sid, a.tg, ex=new_ex, lv='b')
    svc = get_service(sid)
    if svc and a.lv != 'b':
        # 封印号续期即解封
        await svc.emby_change_policy(a.embyid, disable=False)
    LOGGER.info(f'【xserver管理】{msg.from_user.id} 续期 {sid}/{a.name} +{days}天 -> {new_ex}')
    await sendMessage(msg, f'✅ `{a.name}` 已{"续期" if days > 0 else "调整"} {days} 天\n'
                          f'新到期：{new_ex}')
    try:
        await bot.send_message(a.tg, f'🧪 你的测试服 `{a.name}` 到期时间已调整为 {new_ex}')
    except Exception:
        pass


@bot.on_message(filters.command('xsrm', prefixes) & admins_on_filter & filters.private)
async def xs_admin_remove(_, msg):
    await deleteMessage(msg)
    args, sid = _parse_sid_tail(msg.command)
    if len(args) != 1:
        return await sendMessage(msg, '用法：`/xsrm <用户名|tg> [server_id]`', timer=60)
    a = xacc_get_any(sid, _to_key(args[0]))
    if a is None:
        return await sendMessage(msg, f'❌ `{sid}` 未找到 `{args[0]}`', timer=60)
    xc = _xc_of(sid)
    svc = get_service(sid)
    if svc and a.embyid:
        deleted = await svc.x_delete(a.embyid)
        if not deleted:
            ok_u, uinfo = await svc.user(a.embyid)
            gone = (not ok_u) and isinstance(uinfo, dict) and '资源不存在' in uinfo.get('error', '')
            if not gone:
                return await sendMessage(msg, '❌ 远端删除失败（服务器不通？），已中止', timer=60)
    xacc_delete(sid, a.tg)
    if a.embyid:
        xquota_give(sid)
    name = a.name or str(a.tg)
    LOGGER.info(f'【xserver管理】{msg.from_user.id} 删除 {sid}/{name}，名额已回收')
    await sendMessage(msg, f'✅ 已删除 `{name}`（{xc.name if xc else sid}），名额已回收')
    try:
        await bot.send_message(a.tg, f'🧪 管理员删除了你的测试服账号 `{name}`，名额已释放')
    except Exception:
        pass


# ---------------- 账号列表（分页） ----------------

@bot.on_callback_query(filters.regex('^xsa:li:') & admins_on_filter)
async def xs_admin_list(_, call):
    _, _, sid_hex, page_s = call.data.split(':')
    sid = _dec(sid_hex)
    page = int(page_s)
    rows = xacc_all(sid)
    rows.sort(key=lambda r: (r.ex or datetime.max, r.tg))
    per, total_page = 10, max(1, -(-len(rows) // 10))
    page = min(max(page, 0), total_page - 1)
    chunk = rows[page * per:(page + 1) * per]
    q = xquota_get(sid)
    quota_line = f" · 名额 {q.used}/{q.total}" if q else ''
    lines = [f"**▎🧪 账号列表** · 共 {len(rows)}{quota_line}\n"]
    kb = []
    for a in chunk:
        st = {'b': '🟢', 'c': '🔒'}.get(a.lv or 'd', '🎫')
        exp = a.ex.strftime('%m-%d %H:%M') if a.ex else '-'
        lines.append(f"{st} `{a.tg}` {a.name or '（待开号）'} 资格{int(a.us or 0)} 到期{exp}")
        kb.append([(f'👤 {a.tg}', f'xsa:u:{sid_hex}:{a.tg}')])
    nav = []
    if page > 0:
        nav.append(('⬅️', f'xsa:li:{sid_hex}:{page - 1}'))
    if page < total_page - 1:
        nav.append(('➡️', f'xsa:li:{sid_hex}:{page + 1}'))
    nav.append(('🔙 主页', f'xsa:h:{sid_hex}'))
    kb.append(nav)
    await callAnswer(call, '📋 账号列表')
    await editMessage(call, '\n'.join(lines), buttons=ikb(kb))


@bot.on_callback_query(filters.regex('^xsa:u:') & admins_on_filter)
async def xs_admin_user(_, call):
    _, _, sid_hex, tg_s = call.data.split(':')
    sid, tg = _dec(sid_hex), int(tg_s)
    a = xacc_get(sid, tg)
    if a is None:
        return await callAnswer(call, '❌ 记录不存在', True)
    xc = _xc_of(sid)
    text = (f"**▎🧪 {xc.name if xc else sid} · `{a.tg}`**\n"
            f"· ID | `{a.embyid or '待开号'}`\n"
            f"· 用户名 | `{a.name or '-'}`\n"
            f"· 状态 `{a.lv}` · 资格 {int(a.us or 0)} 天\n"
            f"· 到期 | {a.ex or '-'}")
    btn = []
    if a.embyid:
        if a.lv == 'b':
            btn.append(('🔒 封印', f'xsa:od:{sid_hex}:{tg}'))
        else:
            btn.append(('🔓 解封', f'xsa:oe:{sid_hex}:{tg}'))
        btn.append(('🗑 删除', f'xsa:ox:{sid_hex}:{tg}'))
    btn.append(('🔙 列表', f'xsa:li:{sid_hex}:0'))
    await editMessage(call, text, buttons=ikb([btn]))


@bot.on_callback_query(filters.regex('^xsa:od:') & admins_on_filter)
async def xs_admin_disable(_, call):
    _, _, sid_hex, tg_s = call.data.split(':')
    sid, tg = _dec(sid_hex), int(tg_s)
    a = xacc_get(sid, tg)
    if not a or not a.embyid:
        return await callAnswer(call, '❌ 无有效账号', True)
    svc = get_service(sid)
    if svc is None:
        return await callAnswer(call, '⚠️ 服务器未开放', True)
    if await svc.emby_change_policy(a.embyid, disable=True):
        xacc_update(sid, tg, lv='c')
        await callAnswer(call, '🔒 已封印')
    else:
        await callAnswer(call, '❌ 远端策略修改失败', True)


@bot.on_callback_query(filters.regex('^xsa:oe:') & admins_on_filter)
async def xs_admin_enable(_, call):
    _, _, sid_hex, tg_s = call.data.split(':')
    sid, tg = _dec(sid_hex), int(tg_s)
    a = xacc_get(sid, tg)
    if not a or not a.embyid:
        return await callAnswer(call, '❌ 无有效账号', True)
    svc = get_service(sid)
    if svc is None:
        return await callAnswer(call, '⚠️ 服务器未开放', True)
    if await svc.emby_change_policy(a.embyid, disable=False):
        xacc_update(sid, tg, lv='b')
        await callAnswer(call, '🔓 已解封')
    else:
        await callAnswer(call, '❌ 远端策略修改失败', True)


@bot.on_callback_query(filters.regex('^xsa:ox:') & admins_on_filter)
async def xs_admin_delete_cb(_, call):
    _, _, sid_hex, tg_s = call.data.split(':')
    sid, tg = _dec(sid_hex), int(tg_s)
    a = xacc_get(sid, tg)
    if not a or not a.embyid:
        return await callAnswer(call, '❌ 无有效账号', True)
    svc = get_service(sid)
    if svc is None:
        return await callAnswer(call, '⚠️ 服务器未开放', True)
    deleted = await svc.x_delete(a.embyid)
    if not deleted:
        ok_u, uinfo = await svc.user(a.embyid)
        gone = (not ok_u) and isinstance(uinfo, dict) and '资源不存在' in uinfo.get('error', '')
        if not gone:
            return await callAnswer(call, '❌ 远端删除失败，已中止', True)
    xacc_delete(sid, tg)
    xquota_give(sid)
    LOGGER.info(f'【xserver管理】{call.from_user.id} 面板删除 {sid}/{a.name}')
    await editMessage(call, f'✅ 已删除 `{a.name}`，名额已回收',
                      buttons=ikb([[('📋 列表', f'xsa:li:{sid_hex}:0')]]))
