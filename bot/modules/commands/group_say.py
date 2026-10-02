"""管理员私聊选择授权群、预览并以机器人身份发布消息。"""
from dataclasses import dataclass
from secrets import token_hex
from time import monotonic

from pyrogram import filters
from pyrogram.helpers import ikb

from bot import bot, config, prefixes, LOGGER
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.msg_utils import ask_return, callAnswer, editMessage

_DRAFT_TTL = 600
_drafts = {}
_collecting = set()


@dataclass
class GroupDraft:
    token: str
    chat_id: int
    title: str
    preview: object
    created_at: float


def _authorized(update):
    user = update.from_user
    message = getattr(update, 'message', None) or update
    return bool(user and message.chat.id == user.id
                and (user.id == config.owner or user.id in config.admins))


def _groups():
    return list(dict.fromkeys(chat_id for chat_id in config.group if chat_id < 0))


async def _group_title(chat_id):
    try:
        chat = await bot.get_chat(chat_id)
        return chat.title or str(chat_id)
    except Exception as e:
        LOGGER.warning(f'【群内发言】读取群名称失败 chat={chat_id}: {e}')
        return str(chat_id)


async def _menu(update):
    if not _authorized(update):
        return
    message = getattr(update, 'message', None) or update
    if update.from_user.id in _collecting:
        return await message.reply('请先发送正在等待的消息，或发送 /cancel 取消。')
    _drafts.pop(update.from_user.id, None)
    groups = _groups()
    if not groups:
        return await message.reply('⚠️ 尚未配置可发送的授权群。')
    rows = [[(await _group_title(chat_id), f'gs:pick:{chat_id}')] for chat_id in groups]
    await message.reply('📣 群内发言\n\n请选择要发布消息的群：', reply_markup=ikb(rows))


@bot.on_message(filters.command('groupsay', prefixes) & admins_on_filter & filters.private)
async def group_say_command(_, msg):
    await _menu(msg)


@bot.on_callback_query(filters.regex('^gs:menu$') & admins_on_filter)
async def group_say_menu(_, call):
    if not _authorized(call):
        return await callAnswer(call, '请在私聊中使用管理员群内发言。', True)
    await callAnswer(call, '📣 群内发言')
    await _menu(call)


@bot.on_callback_query(filters.regex(r'^gs:pick:-\d+$') & admins_on_filter)
async def group_say_pick(_, call):
    if not _authorized(call):
        return await callAnswer(call, '请在私聊中使用管理员群内发言。', True)
    chat_id = int(call.data.split(':')[2])
    if chat_id not in _groups():
        return await callAnswer(call, '该群已不在授权群配置中。', True)
    uid = call.from_user.id
    if uid in _collecting:
        return await callAnswer(call, '请先发送消息或 /cancel 取消。', True)
    _collecting.add(uid)
    _drafts.pop(uid, None)
    try:
        await callAnswer(call, '请输入消息')
        title = await _group_title(chat_id)
        msg = await ask_return(call, text=f'📣 发送到：{title}\n\n'
                               '请在 10 分钟内发送文字、单条图片、视频或文件。\n'
                               '发送后先预览，确认后才会发布到群内。\n'
                               '取消请发送 /cancel。', timer=_DRAFT_TTL)
        if not msg:
            return
        if msg.text and msg.text.strip() == '/cancel':
            return await call.message.reply('已取消群内发言。')
        if not _authorized(call) or msg.from_user.id != uid or chat_id not in _groups():
            return await call.message.reply('权限或授权群配置已变化，请重新操作。')
        if msg.media_group_id:
            return await call.message.reply('请发送单条消息，图片合集暂不支持。')
        try:
            # 保存机器人发出的预览副本，确认时发布相同内容。
            preview = await msg.copy(uid, reply_markup=None)
        except Exception as e:
            LOGGER.warning(f'【群内发言】生成预览失败 admin={uid}: {e}')
            return await call.message.reply('❌ 无法预览这条消息，请重新使用 /groupsay。')
        token = token_hex(6)
        _drafts[uid] = GroupDraft(token, chat_id, title, preview, monotonic())
        await call.message.reply(f'📣 上方为待发布消息\n目标群：{title}\n\n'
                                 '请在 10 分钟内确认发送。', reply_markup=ikb([
                                     [('✅ 确认发送', f'gs:send:{token}'),
                                      ('❌ 取消', f'gs:cancel:{token}')]]))
    finally:
        _collecting.discard(uid)


@bot.on_callback_query(filters.regex(r'^gs:(send|cancel):[0-9a-f]{12}$') & admins_on_filter)
async def group_say_confirm(_, call):
    if not _authorized(call):
        return await callAnswer(call, '请在私聊中使用管理员群内发言。', True)
    _, action, token = call.data.split(':')
    uid = call.from_user.id
    draft = _drafts.get(uid)
    if draft is None or draft.token != token:
        return await callAnswer(call, '草稿已失效，请重新使用 /groupsay。', True)
    if monotonic() - draft.created_at >= _DRAFT_TTL:
        _drafts.pop(uid)
        await callAnswer(call, '草稿已过期')
        return await editMessage(call, '⌛ 草稿已过期，请重新使用 /groupsay。')
    # 在发送前消费草稿，重复点击不会重复发布。
    _drafts.pop(uid)
    if action == 'cancel':
        await callAnswer(call, '已取消')
        return await editMessage(call, '已取消群内发言。')
    if draft.chat_id not in _groups():
        await callAnswer(call, '授权群配置已变化', True)
        return await editMessage(call, '该群已不在授权群配置中，消息未发布。')
    await callAnswer(call, '正在发送')
    try:
        sent = await draft.preview.copy(draft.chat_id, reply_markup=None)
    except Exception as e:
        LOGGER.error(f'【群内发言】发布失败 admin={uid} chat={draft.chat_id}: {e}')
        return await editMessage(call, '❌ 未能确认消息发送成功。请先查看目标群，'
                                 '并检查机器人发言权限，再使用 /groupsay 重新操作。')
    LOGGER.info(f'【群内发言】admin={uid} chat={draft.chat_id} message={sent.id}')
    await editMessage(call, f'✅ 已发送到：{draft.title}')
