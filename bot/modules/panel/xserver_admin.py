"""
xserver（测试服）管理面板 —— 全部走新表新文件，不触碰主服管理逻辑。

双名额池管理：main（主服已有账号用户）与 open（无主服账号用户）各自独立
➕/➖/设总数；账号行记录占用的池，删除/注销按原池回收。

入口：
- 私聊 `!xspanel`                    管理主页（名额/通道开关/开码/账号列表）
- `!xsin  <tg|用户名> [server_id]`   查询账号
- `!xsgr  <tg> <天数> [server_id]`   直接发放开号资格
- `!xsext <用户名|tg> <天数> [server_id]`  续期（封印号顺延即解封）
- `!xsrm  <用户名|tg> [server_id]`   删除账号并回收名额
- `!xscrn <数量> <天数> [server_id]` 开续期码（资格码走面板按钮）

回调前缀 `xsa:`，server_id 一律 hex 编码；名额类回调携带 pool 段（main|open）。
"""
import re
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
                                        xquota_set_total, xquota_give, xcode_add,
                                        xcode_unused_of, xquota_pool_used,
                                        xquota_pool_total, POOL_MAIN, POOL_OPEN)
from bot.modules.panel.xserver_panel import _enc, _dec, _xc_of

POOL_LABEL = {POOL_MAIN: '主服用户', POOL_OPEN: '开放池'}


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

async def _command_reply(msg, text: str, buttons=None):
    """先回复管理命令，再删除原命令，避免回复目标失效。"""
    result = await sendMessage(msg, text, buttons=buttons)
    await deleteMessage(msg)
    return result


# ---------------- 面板主页 ----------------

def _admin_home_text(xc, q) -> str:
    ch = xc.channels
    on = lambda b: '✅' if b else '❎'  # noqa: E731
    if q:
        pm_u, pm_t = xquota_pool_used(q, POOL_MAIN), xquota_pool_total(q, POOL_MAIN)
        po_u, po_t = xquota_pool_used(q, POOL_OPEN), xquota_pool_total(q, POOL_OPEN)
        quota_block = (f"· 名额·主服用户 | {pm_u}/{pm_t}（剩 {max(0, pm_t - pm_u)}）\n"
                       f"· 名额·开放池   | {po_u}/{po_t}（剩 {max(0, po_t - po_u)}）\n")
    else:
        quota_block = "· 名额 | 未初始化\n"
    code_block = (f"· 未使用资格码 | {xcode_unused_of(xc.id, 'reg')}\n"
                  f"· 未使用续期码 | {xcode_unused_of(xc.id, 'renew')}\n")
    return (f"**▎🧪 {xc.name} · 管理**\n\n"
            f"· 地址 | `{xc.url}`\n"
            f"· 线路 | {xc.line}\n"
            f"{quota_block}"
            f"{code_block}"
            f"· 有效期 | {xc.expire_days} 天 · 前缀 `{xc.name_prefix}` · 并发 {xc.limit}\n"
            f"· 复开策略 | {'🔁 允许再次开号' if xc.allow_reopen else '🎫 每人仅限一次（防蹲坑）'}\n\n"
            f"**开号通道**\n"
            f"{on(ch.main_user)} 主服一键 · {on(ch.whitelist)} 白名单免费 · "
            f"{on(ch.points)} 积分 {ch.points_cost}{sakura_b}")


def _admin_home_ikb(sid: str, xc) -> list:
    ch = xc.channels
    dot = lambda b: '🟢' if b else '🔴'  # noqa: E731
    eh = _enc(sid)
    return [
        [(f'➖ 主服池', f'xsa:qm:{eh}:{POOL_MAIN}'),
         (f'➕ 主服池', f'xsa:qp:{eh}:{POOL_MAIN}'),
         (f'🎚 主服总数', f'xsa:qt:{eh}:{POOL_MAIN}')],
        [(f'➖ 开放池', f'xsa:qm:{eh}:{POOL_OPEN}'),
         (f'➕ 开放池', f'xsa:qp:{eh}:{POOL_OPEN}'),
         (f'🎚 开放总数', f'xsa:qt:{eh}:{POOL_OPEN}')],
        [(f'{dot(ch.main_user)}主服一键', f'xsa:tc:{eh}:main_user'),
         (f'{dot(ch.whitelist)}白名单', f'xsa:tc:{eh}:whitelist'),
         (f'{dot(ch.points)}积分兑换', f'xsa:tc:{eh}:points')],
        [(f'💰 改单价{ch.points_cost}', f'xsa:pc:{eh}'),
         (f'{"🟢" if xc.allow_reopen else "🔴"}允许复开', f'xsa:ro:{eh}'),
         ('⚙️ 参数配置', f'xsa:cfg:{eh}')],
        [('🎫 开资格码', f'xsa:cr:{eh}'), ('📋 账号列表', f'xsa:li:{eh}:0')],
        [('🔙 返回', 'manage')],
    ]

async def _admin_home_render(_, update, sid: str):
    xc = await _xc_any(sid)  # 管理侧允许已禁用的服务器
    if xc is None:
        if isinstance(update, CallbackQuery):
            return await callAnswer(update, '⚠️ 服务器不存在', True)
        return await sendMessage(update, '⚠️ 服务器不存在', timer=60)
    if xc.enable:
        get_service(sid)  # 确保 quota 种子
    q = xquota_get(sid)
    text = _admin_home_text(xc, q)
    buttons = ikb(_admin_home_ikb(sid, xc))
    if isinstance(update, CallbackQuery):
        await callAnswer(update, f'🧪 {xc.name}')
        return await editMessage(update, text, buttons=buttons)
    return await sendMessage(update, text, buttons=buttons)

def _all_configured_servers():
    return list(config.xservers or [])


def _server_rows(servers):
    return [[(f'{"🧪" if xc.enable else "🔴"} {xc.name}', f'xsa:h:{_enc(xc.id)}')]
            for xc in servers]


@bot.on_message(filters.command('xspanel', prefixes) & admins_on_filter & filters.private)
async def xs_admin_cmd(_, msg):
    servers = _all_configured_servers()
    if not servers:
        result = await sendMessage(msg, '⚠️ 尚未配置测试服（config.xservers）', timer=60)
    elif len(servers) == 1:
        result = await _admin_home_render(_, msg, servers[0].id)
    else:
        rows = _server_rows(servers)
        result = await sendMessage(msg, '**🧪 测试服管理 · 选择服务器**（🔴=已禁用）',
                                   buttons=ikb(rows))
    await deleteMessage(msg)
    return result


@bot.on_callback_query(filters.regex('^xsa:m$') & admins_on_filter)
async def xs_admin_menu_cb(_, call):
    servers = _all_configured_servers()
    if not servers:
        return await callAnswer(call, '⚠️ 无配置的测试服', True)
    if len(servers) == 1:
        return await _admin_home_render(_, call, servers[0].id)
    await editMessage(call, '**🧪 测试服管理 · 选择服务器**（🔴=已禁用）',
                      buttons=ikb(_server_rows(servers)))


@bot.on_callback_query(filters.regex('^xsa:h:') & admins_on_filter)
async def xs_admin_home_cb(_, call):
    return await _admin_home_render(_, call, _dec(call.data.split(':')[2]))


# ---------------- ⚙️ 参数配置页 ----------------

async def _xc_any(sid: str):
    """管理侧解析：允许已禁用的服务器（否则无法解禁）"""
    for xc in config.xservers or []:
        if xc.id == sid:
            return xc
    return None


def _settings_text(xc) -> str:
    bl = '、'.join(xc.block_libs) if xc.block_libs else '（无）'
    return (f"**▎⚙️ {xc.name} · 参数配置**\n\n"
            f"{'🟢 已启用' if xc.enable else '🔴 已禁用（用户侧不可见）'}\n\n"
            f"· 显示名 | {xc.name}\n"
            f"· 管理地址 url | `{xc.url}`\n"
            f"· API Key | `{'已设置' if xc.api else '⚠️ 空'}`\n"
            f"· 用户线路 line | {xc.line or '（空）'}\n"
            f"· 有效天数 expire_days | {xc.expire_days}\n"
            f"· 并发上限 limit | {xc.limit}\n"
            f"· 用户名前缀 name_prefix | `{xc.name_prefix}`\n"
            f"· 隐藏媒体库 block_libs | {bl}\n\n"
            f"_点击对应按钮修改；url/api 修改后自动重建连接，即时生效_")


def _settings_ikb(sid: str, xc) -> list:
    eh = _enc(sid)
    return [
        [(f'{"🟢" if xc.enable else "🔴"} 启用/禁用', f'xsa:se:{eh}')],
        [('📛 显示名', f'xsa:cf:{eh}:name'), ('🌐 线路line', f'xsa:cf:{eh}:line')],
        [('🔗 地址url', f'xsa:cf:{eh}:url'), ('🔑 APIKey', f'xsa:cf:{eh}:api')],
        [(f'⏳ 天数({xc.expire_days})', f'xsa:cf:{eh}:expire_days'),
         (f'📡 并发({xc.limit})', f'xsa:cf:{eh}:limit')],
        [(f'🏷 前缀({xc.name_prefix})', f'xsa:cf:{eh}:name_prefix')],
        [('🚫 隐藏库', f'xsa:cf:{eh}:block_libs')],
        [('🔙 返回主页', f'xsa:h:{eh}')],
    ]


@bot.on_callback_query(filters.regex('^xsa:cfg:') & admins_on_filter)
async def xs_settings_cb(_, call):
    sid = _dec(call.data.split(':')[2])
    xc = await _xc_any(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 服务器不存在', True)
    await callAnswer(call, '⚙️ 参数配置')
    await editMessage(call, _settings_text(xc), buttons=ikb(_settings_ikb(sid, xc)))


@bot.on_callback_query(filters.regex('^xsa:se:') & admins_on_filter)
async def xs_toggle_enable(_, call):
    sid = _dec(call.data.split(':')[2])
    xc = await _xc_any(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 服务器不存在', True)
    xc.enable = not xc.enable
    save_config()
    LOGGER.info(f'【xserver管理】{call.from_user.id} {"启用" if xc.enable else "禁用"} 服务器 {sid}')
    if xc.enable:
        get_service(sid)  # 重建连接 + quota 种子
    await editMessage(call, f'{"🟢 已启用" if xc.enable else "🔴 已禁用"} `{sid}`',
                      buttons=ikb([[('⚙️ 继续配置', f'xsa:cfg:{_enc(sid)}')],
                                   [('🔙 主页', 'xsa:m')]]))


_SET_HINTS = {
    'name': ('📛 发送新的**显示名**（≤12字）', str, None),
    'url': ('🔗 发送新的**管理地址**，例 `http://10.0.0.2:8096`', str,
            lambda v: v.startswith(('http://', 'https://')) or '仅支持 http(s):// 开头'),
    'api': ('🔑 发送新的 **Emby API Key**', str, None),
    'line': ('🌐 发送新的**用户线路展示文本**（可多行）', str, None),
    'expire_days': ('⏳ 发送新的**有效天数**（1-3650 整数）', int,
                    lambda v: 1 <= v <= 3650 or '天数须在 1-3650 之间'),
    'limit': ('📡 发送新的**同时连接数**（1-10 整数）', int,
              lambda v: 1 <= v <= 10 or '并发须在 1-10 之间'),
    'name_prefix': ('🏷 发送新的**用户名前缀**（≤8字符，不含空格，例 `t_`）', str,
                    lambda v: (len(v) <= 8 and
                               not re.search(r'[\s~`*\[\]()#+\\|<>:/?@&%$^{}=";\']', v))
                    or '前缀不能超过8字符，且不能含空格/特殊字符'),
    'block_libs': ('🚫 发送要隐藏的媒体库名称，**逗号分隔**；发送 `-` 清空\n'
                   '例：`nsfw, 测试库`', str, None),
}


@bot.on_callback_query(filters.regex('^xsa:cf:') & admins_on_filter)
async def xs_edit_field(_, call):
    parts = call.data.split(':')
    sid_hex, field = parts[2], parts[3]
    if field not in _SET_HINTS:
        return await callAnswer(call, '⚠️ 未知字段', True)
    sid = _dec(sid_hex)
    xc = await _xc_any(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 服务器不存在', True)
    hint, caster, validator = _SET_HINTS[field]
    shown = '（已设置，输入新值将覆盖）' if field == 'api' and xc.api else getattr(xc, field)
    msg = await ask_return(call, text=f'{hint}\n\n当前值：`{shown}`\n\n取消 /cancel',
                           timer=120)
    if not msg:
        return
    if msg.text.strip() == '/cancel':
        return await editMessage(call, _settings_text(xc), buttons=ikb(_settings_ikb(sid, xc)))
    raw = msg.text
    try:
        if field == 'block_libs':
            newv = [] if raw.strip() == '-' else [s.strip() for s in raw.split(',') if s.strip()]
        elif caster is int:
            newv = int(raw.strip())
        else:
            newv = raw.strip()
            if field == 'name' and len(newv) > 12:
                raise ValueError('显示名不能超过12字')
            if field == 'url':
                newv = newv.rstrip('/')
    except ValueError as e:
        return await sendMessage(call, f'⚠️ {e or "格式错误，请输入整数"}', timer=60)
    if not newv and field in ('url', 'api', 'name'):
        return await sendMessage(call, '⚠️ 该字段不能为空', timer=60)
    if validator:
        check = validator(newv)
        if check is not True:
            return await sendMessage(call, f'⚠️ {check}', timer=60)
    setattr(xc, field, newv)
    save_config()
    if field in ('url', 'api'):
        get_service(sid)  # 签名变化 → registry 重建连接
    LOGGER.info(f'【xserver管理】{call.from_user.id} 修改 {sid}.{field} -> {newv}')
    await sendMessage(call, f'✅ `{field}` 已更新', timer=30)
    await editMessage(call, _settings_text(xc), buttons=ikb(_settings_ikb(sid, xc)))


# ---------------- 名额调整（双池） ----------------

@bot.on_callback_query(filters.regex('^xsa:q[pm]:') & admins_on_filter)
async def xs_quota_add_minus(_, call):
    act, sid_hex, pool = call.data.split(':')[1:4]
    pool = pool if pool in (POOL_MAIN, POOL_OPEN) else POOL_MAIN
    sid = _dec(sid_hex)
    delta = 1 if act == 'qp' else -1
    q = xquota_get(sid)
    if q is None:
        return await callAnswer(call, '⚠️ 名额未初始化', True)
    new_total = xquota_pool_total(q, pool) + delta
    if new_total < xquota_pool_used(q, pool):
        return await callAnswer(call,
                                f'⚠️ {POOL_LABEL[pool]}总名额不得低于已用 '
                                f'{xquota_pool_used(q, pool)}', True)
    if not xquota_set_total(sid, new_total, pool):
        return await callAnswer(call, '❌ 名额更新失败，请检查数据库', True)
    await _admin_home_render(_, call, sid)


@bot.on_callback_query(filters.regex('^xsa:qt:') & admins_on_filter)
async def xs_quota_set(_, call):
    sid_hex, pool = call.data.split(':')[2], call.data.split(':')[3]
    pool = pool if pool in (POOL_MAIN, POOL_OPEN) else POOL_MAIN
    sid = _dec(sid_hex)
    msg = await ask_return(call,
                           text=f'🎚 发送 {POOL_LABEL[pool]} 的**总名额**数字（取消 /cancel）',
                           timer=120)
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
    if q and total < xquota_pool_used(q, pool):
        return await sendMessage(call,
                                 f'⚠️ {POOL_LABEL[pool]}当前已用 {xquota_pool_used(q, pool)}，'
                                 f'总名额不得低于此数', timer=60)
    if not xquota_set_total(sid, total, pool):
        return await callAnswer(call, '❌ 名额更新失败，请检查数据库', True)
    await _admin_home_render(_, call, sid)


# ---------------- 通道开关 & 单价 ----------------

@bot.on_callback_query(filters.regex('^xsa:tc:') & admins_on_filter)
async def xs_toggle_channel(_, call):
    _, _, sid_hex, chan = call.data.split(':')
    if chan not in ('main_user', 'whitelist', 'points'):
        return await callAnswer(call, '⚠️ 未知开号通道', True)
    sid = _dec(sid_hex)
    xc = await _xc_any(sid)
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
    msg = await ask_return(call, text=f'💰 发送新的积分开号单价（{sakura_b}，取消 /cancel）',
                           timer=120)
    if not msg:
        return
    if msg.text.strip() == '/cancel':
        return await _admin_home_render(_, call, sid)
    try:
        cost = int(msg.text.strip())
        assert cost > 0
    except (ValueError, AssertionError):
        return await sendMessage(call, '⚠️ 请发送正整数', timer=60)
    xc = await _xc_any(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 服务器不存在', True)
    xc.channels.points_cost = cost
    save_config()
    LOGGER.info(f'【xserver管理】{call.from_user.id} 设置 {sid} 积分单价 -> {cost}')
    await _admin_home_render(_, call, sid)


@bot.on_callback_query(filters.regex('^xsa:ro:') & admins_on_filter)
async def xs_toggle_reopen(_, call):
    sid = _dec(call.data.split(':')[2])
    xc = await _xc_any(sid)
    if xc is None:
        return await callAnswer(call, '⚠️ 服务器不存在', True)
    xc.allow_reopen = not xc.allow_reopen
    save_config()
    LOGGER.info(f'【xserver管理】{call.from_user.id} 切换 {sid} allow_reopen -> '
                f'{xc.allow_reopen}')
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
    args, sid = _parse_sid_tail(msg.command)
    if len(args) != 2:
        return await _command_reply(msg, '用法：`/xscrn <数量> <天数> [server_id]`')
    try:
        count, days = int(args[0]), int(args[1])
        assert 1 <= count <= 50 and 1 <= days <= 3650
    except (ValueError, AssertionError):
        return await _command_reply(msg, '⚠️ 数量1-50、天数1-3650')
    if _xc_of(sid) is None:
        return await _command_reply(msg, '⚠️ 无启用的测试服')
    result = await _emit_codes(msg, sid, count, days, 'renew')
    await deleteMessage(msg)
    return result


# ---------------- 管理命令：查询/发资格/续期/删除 ----------------

@bot.on_message(filters.command('xsin', prefixes) & admins_on_filter)
async def xs_admin_info(_, msg):
    args, sid = _parse_sid_tail(msg.command)
    if len(args) != 1:
        return await _command_reply(msg, '用法：`/xsin <tg|用户名> [server_id]`')
    if _xc_of(sid) is None:
        return await _command_reply(msg, '⚠️ 无启用的测试服')
    a = xacc_get_any(sid, _to_key(args[0]))
    if a is None:
        return await _command_reply(msg, f'❌ `{sid}` 未找到账号 `{args[0]}`')
    xc = _xc_of(sid)
    q = xquota_get(sid)
    quota_line = ''
    if q:
        quota_line = (f"· 名额·主服用户 | {xquota_pool_used(q, POOL_MAIN)}/{xquota_pool_total(q, POOL_MAIN)}\n"
                      f"· 名额·开放池   | {xquota_pool_used(q, POOL_OPEN)}/{xquota_pool_total(q, POOL_OPEN)}")
    text = (f"**▎🧪 {xc.name} 账号档案**\n\n"
            f"· TG | `{a.tg}`\n"
            f"· EmbyID | `{a.embyid or '（未开号，持资格）'}`\n"
            f"· 用户名 | `{a.name or '-'}`\n"
            f"· 状态 | `{a.lv}` · 资格 {int(a.us or 0)} 天\n"
            f"· 创建 | {a.cr} · 到期 | {a.ex}\n"
            f"· 占用池 | `{POOL_LABEL.get(a.pool or POOL_MAIN, a.pool)}`\n"
            f"{quota_line}")
    return await _command_reply(msg, text, buttons=ikb([[('🔙 主页', 'xsa:m')]]))


@bot.on_message(filters.command('xsgr', prefixes) & admins_on_filter & filters.private)
async def xs_admin_grant(_, msg):
    args, sid = _parse_sid_tail(msg.command)
    if len(args) != 2:
        return await _command_reply(msg, '用法：`/xsgr <tg> <天数> [server_id]`')
    try:
        tg, days = int(args[0]), int(args[1])
        assert days > 0
    except (ValueError, AssertionError):
        return await _command_reply(msg, '⚠️ tg/天数须为正整数')
    if _xc_of(sid) is None:
        return await _command_reply(msg, f'⚠️ 测试服 `{sid}` 未启用')
    if sql_get_emby(tg) is None and xacc_get(sid, tg) is None:
        return await _command_reply(msg, '⚠️ 该 TG 未 /start 过 bot，无法发放资格')
    if not xacc_grant(sid, tg, days):
        return await _command_reply(msg, '❌ 资格写入数据库失败，请稍后重试')
    LOGGER.info(f'【xserver管理】{msg.from_user.id} 给 {tg} 在 {sid} 发放资格 {days} 天')
    await _command_reply(msg, f'✅ 已给 `{tg}` 在 `{sid}` 发放开号资格 **{days}** 天')
    try:
        await bot.send_message(tg, f'🎉 管理员发放了测试服 `{sid}` 开号资格 {days} 天，'
                                   f'前往【🧪 测试服开号】使用')
    except Exception as e:
        LOGGER.warning(f'【xserver管理】资格通知失败 {tg}: {e}')


@bot.on_message(filters.command('xsext', prefixes) & admins_on_filter & filters.private)
async def xs_admin_extend(_, msg):
    args, sid = _parse_sid_tail(msg.command)
    if len(args) != 2:
        return await _command_reply(msg, '用法：`/xsext <用户名|tg> <天数> [server_id]`')
    try:
        days = int(args[1])
        assert days != 0
    except (ValueError, AssertionError):
        return await _command_reply(msg, '⚠️ 天数须为非零整数')
    a = xacc_get_any(sid, _to_key(args[0]))
    if a is None or not a.embyid:
        return await _command_reply(msg, f'❌ `{sid}` 未找到已开通账号 `{args[0]}`')
    svc = get_service(sid)
    if a.lv != 'b':
        if svc is None:
            return await _command_reply(msg, '❌ 服务器未开放，无法解封并续期')
        if not await svc.emby_change_policy(a.embyid, disable=False):
            return await _command_reply(msg, '❌ 远端解封失败，续期已中止')
    base = a.ex if (a.ex and a.ex > datetime.now()) else datetime.now()
    new_ex = base + timedelta(days=days)
    if not xacc_update(sid, a.tg, ex=new_ex, lv='b'):
        if a.lv != 'b' and svc:
            await svc.emby_change_policy(a.embyid, disable=True)
        return await _command_reply(msg, '❌ 到期时间写入数据库失败，续期已中止')
    LOGGER.info(f'【xserver管理】{msg.from_user.id} 续期 {sid}/{a.name} +{days}天 -> {new_ex}')
    await _command_reply(msg, f'✅ `{a.name}` 已{"续期" if days > 0 else "调整"} {days} 天\n'
                              f'新到期：{new_ex}')
    try:
        await bot.send_message(a.tg, f'🧪 你的测试服 `{a.name}` 到期时间已调整为 {new_ex}')
    except Exception as e:
        LOGGER.warning(f'【xserver管理】续期通知失败 {a.tg}: {e}')


@bot.on_message(filters.command('xsrm', prefixes) & admins_on_filter & filters.private)
async def xs_admin_remove(_, msg):
    args, sid = _parse_sid_tail(msg.command)
    if len(args) != 1:
        return await _command_reply(msg, '用法：`/xsrm <用户名|tg> [server_id]`')
    a = xacc_get_any(sid, _to_key(args[0]))
    if a is None:
        return await _command_reply(msg, f'❌ `{sid}` 未找到 `{args[0]}`')
    xc = _xc_of(sid)
    svc = get_service(sid)
    if a.embyid:
        if svc is None:
            return await _command_reply(msg, '❌ 服务器未开放，已中止删除，避免遗留远端账号')
        deleted = await svc.x_delete(a.embyid)
        if not deleted:
            ok_u, uinfo = await svc.user(a.embyid)
            gone = (not ok_u) and isinstance(uinfo, dict) and '资源不存在' in uinfo.get('error', '')
            if not gone:
                return await _command_reply(msg, '❌ 远端删除失败（服务器不通？），已中止')
    if not xacc_delete(sid, a.tg):
        return await _command_reply(msg, '❌ 本地记录删除失败，请重试')
    pool = a.pool or POOL_MAIN
    if a.embyid and not xquota_give(sid, pool):
        LOGGER.warning(f'【xserver管理】删除 {sid}/{a.name} 后名额计数未减少：{pool}')
    name = a.name or str(a.tg)
    LOGGER.info(f'【xserver管理】{msg.from_user.id} 删除 {sid}/{name}，名额已退回{POOL_LABEL[pool]}')
    await _command_reply(msg, f'✅ 已删除 `{name}`（{xc.name if xc else sid}），'
                              f'名额已退回{POOL_LABEL[pool]}')
    try:
        await bot.send_message(a.tg, f'🧪 管理员删除了你的测试服账号 `{name}`，名额已释放')
    except Exception as e:
        LOGGER.warning(f'【xserver管理】删除通知失败 {a.tg}: {e}')


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
    quota_line = ''
    if q:
        quota_line = (f" 主服 {xquota_pool_used(q, POOL_MAIN)}/{xquota_pool_total(q, POOL_MAIN)}"
                      f" 开放 {xquota_pool_used(q, POOL_OPEN)}/{xquota_pool_total(q, POOL_OPEN)}")
    lines = [f"**▎🧪 账号列表** · 共 {len(rows)}{quota_line}\n"]
    kb = []
    for a in chunk:
        st = {'b': '🟢', 'c': '🔒'}.get(a.lv or 'd', '🎫')
        exp = a.ex.strftime('%m-%d %H:%M') if a.ex else '-'
        pool_tag = '开' if (a.pool == POOL_OPEN) else ('主' if a.pool == POOL_MAIN else '·')
        lines.append(f"{st}[{pool_tag}] `{a.tg}` {a.name or '（待开号）'} "
                     f"资格{int(a.us or 0)} 到期{exp}")
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
            f"· 登录密码 | `{a.pwd or '-'}`\n"
            f"· 安全密码 | `{a.pwd2 or '-'}`\n"
            f"· 状态 `{a.lv}` · 资格 {int(a.us or 0)} 天\n"
            f"· 到期 | {a.ex or '-'}\n"
            f"· 占用池 | `{POOL_LABEL.get(a.pool or POOL_MAIN, a.pool)}`")
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
    if not await svc.emby_change_policy(a.embyid, disable=True):
        return await callAnswer(call, '❌ 远端策略修改失败', True)
    if not xacc_update(sid, tg, lv='c'):
        await svc.emby_change_policy(a.embyid, disable=False)
        return await callAnswer(call, '❌ 本地状态写入失败，已回滚远端策略', True)
    await callAnswer(call, '🔒 已封印')
    await xs_admin_user(_, call)


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
    if not await svc.emby_change_policy(a.embyid, disable=False):
        return await callAnswer(call, '❌ 远端策略修改失败', True)
    if not xacc_update(sid, tg, lv='b'):
        await svc.emby_change_policy(a.embyid, disable=True)
        return await callAnswer(call, '❌ 本地状态写入失败，已回滚远端策略', True)
    await callAnswer(call, '🔓 已解封')
    await xs_admin_user(_, call)


@bot.on_callback_query(filters.regex('^xsa:ox:') & admins_on_filter)
async def xs_admin_delete_ask(_, call):
    _, _, sid_hex, tg_s = call.data.split(':')
    sid, tg = _dec(sid_hex), int(tg_s)
    a = xacc_get(sid, tg)
    if not a or not a.embyid:
        return await callAnswer(call, '❌ 无有效账号', True)
    await callAnswer(call, '⚠️ 请确认删除')
    await editMessage(
        call,
        f'**确认删除测试服账号？**\n\n用户名：`{a.name}`\nTG：`{a.tg}`\n'
        '远端账号、本地记录及所占名额都会被清理。',
        buttons=ikb([
            [('✅ 确认删除', f'xsa:oxc:{sid_hex}:{tg}')],
            [('🔙 取消', f'xsa:u:{sid_hex}:{tg}')],
        ]),
    )


@bot.on_callback_query(filters.regex('^xsa:oxc:') & admins_on_filter)
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
    if not xacc_delete(sid, tg):
        return await callAnswer(call, '❌ 本地记录删除失败，请重试', True)
    if not xquota_give(sid, a.pool or POOL_MAIN):
        LOGGER.warning(f'【xserver管理】面板删除 {sid}/{a.name} 后名额计数未减少')
    LOGGER.info(f'【xserver管理】{call.from_user.id} 面板删除 {sid}/{a.name}')
    await editMessage(call, f'✅ 已删除 `{a.name}`，名额已退回原池',
                      buttons=ikb([[('📋 列表', f'xsa:li:{sid_hex}:0')]]))
