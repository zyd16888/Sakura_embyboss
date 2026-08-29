#!/usr/bin/python3
from pyrogram.errors import BadRequest
from pyrogram.filters import create
from bot import admins, owner, group, LOGGER
from pyrogram.enums import ChatMemberStatus


_MEMBER_STATUSES = {
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.OWNER,
}


def _user_id(update):
    user = update.from_user or update.sender_chat
    return user.id if user else None


async def _answer_membership_failure(update, text: str):
    answer = getattr(update, 'answer', None)
    if answer is None:
        return
    try:
        await answer(text, show_alert=True)
    except Exception as e:
        LOGGER.warning(f"群组资格失败提示发送失败: {e}")


async def _is_authorized_group_user(client, update, notify=False) -> bool:
    uid = _user_id(update)
    if uid is None:
        return False
    if uid == owner or uid in admins:
        return True

    lookup_failed = False
    for chat_id in group:
        try:
            member = await client.get_chat_member(chat_id=int(chat_id), user_id=uid)
        except BadRequest as e:
            error_id = getattr(e, 'ID', '')
            if error_id == 'USER_NOT_PARTICIPANT':
                continue
            lookup_failed = True
            if error_id == 'CHAT_ADMIN_REQUIRED':
                LOGGER.error(f"bot不能在 {chat_id} 中工作，请检查bot是否在群组及其权限设置")
            else:
                LOGGER.warning(f"检查用户 {uid} 的群组 {chat_id} 资格失败: {e}")
            continue
        except Exception as e:
            lookup_failed = True
            LOGGER.warning(f"检查用户 {uid} 的群组 {chat_id} 资格异常: {e}")
            continue

        if member.status in _MEMBER_STATUSES:
            return True
        if (member.status == ChatMemberStatus.RESTRICTED
                and getattr(member, 'is_member', True)):
            return True

    if notify:
        text = ('⚠️ 群组资格检查暂时失败，请稍后重试。' if lookup_failed else
                '⚠️ 请先加入授权群组后再使用机器人。')
        await _answer_membership_failure(update, text)
    return False


async def admins_on_filter(filt, client, update) -> bool:
    """过滤 admins 中的 ID，包括 owner。"""
    uid = _user_id(update)
    return bool(uid == owner or uid in admins)


async def admins_filter(update):
    """过滤 admins 中的 ID，包括 owner。"""
    uid = _user_id(update)
    return bool(uid == owner or uid in admins)


async def user_in_group_filter(client, update):
    """供函数直接调用的授权群成员检查。"""
    return await _is_authorized_group_user(client, update)


async def user_in_group_on_filter(filt, client, update):
    """供 Pyrogram handler 使用；拒绝回调时给用户明确提示。"""
    return await _is_authorized_group_user(client, update, notify=True)

# 过滤 on_message or on_callback 的admin
admins_on_filter = create(admins_on_filter)
admins_filter = create(admins_filter)

# 过滤 是否在群内
user_in_group_f = create(user_in_group_filter)
user_in_group_on_filter = create(user_in_group_on_filter)
