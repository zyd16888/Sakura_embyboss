"""群内发言离线验证：权限、预览、草稿归属及重复点击，不连接 Telegram。"""
import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


class Filter:
    def __and__(self, other):
        return self


class Client:
    def on_message(self, *args, **kwargs):
        return lambda fn: fn

    on_callback_query = on_message

    async def get_chat(self, chat_id):
        return types.SimpleNamespace(title=f"Group{chat_id}")


config = types.SimpleNamespace(owner=1, admins=[2], group=[-1001, -1002])
client = Client()
bot_stub = types.ModuleType("bot")
bot_stub.bot = client
bot_stub.config = config
bot_stub.prefixes = ['/']
bot_stub.LOGGER = types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                       error=lambda *a: None)
sys.modules['bot'] = bot_stub
pyrogram = types.ModuleType('pyrogram')
pyrogram.filters = types.SimpleNamespace(command=lambda *a: Filter(), regex=lambda *a: Filter(),
                                         private=Filter())
sys.modules['pyrogram'] = pyrogram
helpers = types.ModuleType('pyrogram.helpers')
helpers.ikb = lambda rows: rows
sys.modules['pyrogram.helpers'] = helpers
filter_stub = types.ModuleType('bot.func_helper.filters')
filter_stub.admins_on_filter = Filter()
sys.modules['bot.func_helper.filters'] = filter_stub
msg_stub = types.ModuleType('bot.func_helper.msg_utils')
events = []
copies = []
answer_message = None


async def ask_return(*args, **kwargs):
    return answer_message


async def call_answer(call, text, alert=False):
    events.append(('answer', text))


async def edit_message(call, text):
    events.append(('edit', text))


msg_stub.ask_return = ask_return
msg_stub.callAnswer = call_answer
msg_stub.editMessage = edit_message
sys.modules['bot.func_helper.msg_utils'] = msg_stub
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('group_say_under_test',
                                            ROOT / 'bot/modules/commands/group_say.py')
gs = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gs
spec.loader.exec_module(gs)


class Message:
    def __init__(self, uid=2, text="Hello", chat_id=None, media_group_id=None):
        self.id = 10
        self.from_user = types.SimpleNamespace(id=uid)
        self.chat = types.SimpleNamespace(id=chat_id if chat_id is not None else uid)
        self.text = text
        self.media_group_id = media_group_id
        self.entities = [('bold', 0, 5)]
        self.copy_error = None

    async def reply(self, text, **kwargs):
        events.append(('reply', text, kwargs))
        return Message()

    async def copy(self, chat_id, **kwargs):
        if self.copy_error:
            raise self.copy_error
        copies.append((chat_id, self.text, self.entities, kwargs))
        preview = Message(text=self.text, chat_id=chat_id)
        preview.entities = list(self.entities)
        return preview


def call(uid=2, data='gs:pick:-1001', chat_id=None):
    return types.SimpleNamespace(from_user=types.SimpleNamespace(id=uid), data=data,
                                 message=Message(uid=uid, chat_id=chat_id))


def run(coro):
    return asyncio.run(coro)


class GroupSayTests(unittest.TestCase):
    def setUp(self):
        global answer_message
        config.admins = [2]
        config.group = [-1001, -1002]
        events.clear()
        copies.clear()
        gs._drafts.clear()
        gs._collecting.clear()
        gs.ask_return = ask_return
        answer_message = Message()

    def draft(self):
        run(gs.group_say_pick(None, call()))
        draft = gs._drafts[2]
        return draft, call(data=f'gs:send:{draft.token}')

    def test_menu_lists_only_configured_groups(self):
        config.group = [-1001, -1001, 2, -1002]
        run(gs.group_say_command(None, Message()))
        buttons = events[-1][2]['reply_markup']
        self.assertEqual(buttons, [[('Group-1001', 'gs:pick:-1001')],
                                   [('Group-1002', 'gs:pick:-1002')]])

    def test_non_admin_and_group_context_cannot_start(self):
        run(gs.group_say_command(None, Message(uid=3)))
        run(gs.group_say_pick(None, call(uid=3)))
        run(gs.group_say_pick(None, call(chat_id=-1001)))
        self.assertEqual(copies, [])
        self.assertEqual(gs._drafts, {})

    def test_preview_then_publish_same_content_as_bot_once(self):
        draft, confirmation = self.draft()
        self.assertEqual([item[0] for item in copies], [2])
        # 管理员后续编辑原始消息不会改变已预览的待发布内容。
        answer_message.text = 'Changed after preview'
        run(gs.group_say_confirm(None, confirmation))
        run(gs.group_say_confirm(None, confirmation))
        self.assertEqual([item[0] for item in copies], [2, -1001])
        self.assertEqual(copies[-1][1:3], ('Hello', [('bold', 0, 5)]))
        self.assertEqual(copies[-1][3], {'reply_markup': None})
        self.assertTrue(any('已发送到' in event[1] for event in events))
        self.assertNotIn(2, gs._drafts)

    def test_other_admin_cannot_publish_someone_elses_draft(self):
        config.admins.append(3)
        draft, _ = self.draft()
        run(gs.group_say_confirm(None, call(uid=3, data=f'gs:send:{draft.token}')))
        self.assertEqual([item[0] for item in copies], [2])
        self.assertIn(2, gs._drafts)

    def test_revoked_admin_cannot_publish(self):
        _, confirmation = self.draft()
        config.admins.clear()
        run(gs.group_say_confirm(None, confirmation))
        self.assertEqual([item[0] for item in copies], [2])

    def test_removed_or_unconfigured_group_cannot_receive_message(self):
        run(gs.group_say_pick(None, call(data='gs:pick:-1009')))
        self.assertEqual(copies, [])
        _, confirmation = self.draft()
        config.group = [-1002]
        run(gs.group_say_confirm(None, confirmation))
        self.assertEqual([item[0] for item in copies], [2])
        self.assertTrue(any('消息未发布' in event[1] for event in events))

    def test_expired_and_cancelled_drafts_are_not_published(self):
        draft, confirmation = self.draft()
        draft.created_at -= 601
        run(gs.group_say_confirm(None, confirmation))
        self.assertNotIn(2, gs._drafts)
        draft, _ = self.draft()
        run(gs.group_say_confirm(None, call(data=f'gs:cancel:{draft.token}')))
        self.assertEqual([item[0] for item in copies], [2, 2])
        self.assertNotIn(2, gs._drafts)

    def test_input_cancel_and_album_do_not_create_draft(self):
        global answer_message
        answer_message = Message(text='/cancel')
        run(gs.group_say_pick(None, call()))
        answer_message = Message(text=None, media_group_id='album')
        run(gs.group_say_pick(None, call()))
        self.assertEqual(copies, [])
        self.assertEqual(gs._drafts, {})
        self.assertEqual(gs._collecting, set())

    def test_single_media_message_is_copied(self):
        global answer_message
        answer_message = Message(text=None)
        _, confirmation = self.draft()
        run(gs.group_say_confirm(None, confirmation))
        self.assertEqual([item[0] for item in copies], [2, -1001])

    def test_send_failure_does_not_report_success_or_retry(self):
        draft, confirmation = self.draft()
        draft.preview.copy_error = RuntimeError('CHAT_WRITE_FORBIDDEN')
        run(gs.group_say_confirm(None, confirmation))
        run(gs.group_say_confirm(None, confirmation))
        self.assertEqual([item[0] for item in copies], [2])
        self.assertFalse(any('已发送到' in event[1] for event in events))
        self.assertTrue(any('未能确认' in event[1] for event in events))

    def test_concurrent_clicks_do_not_duplicate_send(self):
        _, confirmation = self.draft()
        async def click_twice():
            await asyncio.gather(gs.group_say_confirm(None, confirmation),
                                 gs.group_say_confirm(None, confirmation))
        run(click_twice())
        self.assertEqual([item[0] for item in copies], [2, -1001])

    def test_repeated_group_selection_does_not_open_multiple_listeners(self):
        async def slow_answer(*args, **kwargs):
            await gs.group_say_pick(None, call())
            return answer_message
        gs.ask_return = slow_answer
        run(gs.group_say_pick(None, call()))
        self.assertEqual([item[0] for item in copies], [2])
        self.assertEqual(gs._collecting, set())
        self.assertTrue(any('请先发送消息或' in event[1] for event in events))


if __name__ == '__main__':
    unittest.main(verbosity=2)
